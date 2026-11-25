"""No paint forks git: the checkout decoration is cached per workspace.

A sidebar row's checkout icon came from `inspect_linked_checkout`, which spends up
to SIX `git` invocations per row at two seconds each. On a 14-session frame over 11
distinct checkouts that was 116 ms of `git` per frame — and a window switch renders
the same frame three times, in three processes (`nav`'s pre-paint, the
`session-window-changed` hook, and the destination's own first tick), so a switch
paid it three times. The 5 s cache the provider already had could not help: it lived
on the provider INSTANCE, and `canonical_frame` builds a fresh registry per call.

These tests pin the two halves of the fix and the semantics that make it safe:

* **No paint path probes.** `CheckoutProvider` reads; only `CheckoutRefresher`,
  in a visible renderer's own thread, probes.
* **A miss is not an observation.** Nothing is drawn rather than falling through to
  a session record's `worktree_ahead`, which would paint `shipped` over a checkout
  that has since gained unmerged work.
* **Inside the reuse age the observation is drawn as it stands**, `shipped`
  included — the user's decision, against a blinking empty column.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from hive_ide.checkout_cache import (
    CheckoutCache,
    CheckoutCacheReader,
    CheckoutObservation,
    CheckoutRefresher,
)
from hive_ide.git_status import GitCheckoutStatus
from hive_ide.sidebar_plugins import CheckoutProvider


@pytest.fixture
def workspace(tmp_path):
    directory = tmp_path / "workspace"
    directory.mkdir()
    return directory


@pytest.fixture
def state_home(tmp_path):
    return tmp_path / "state"


def _session(working_dir, workspace_key, **extra) -> dict:
    record = {
        "id": "s1",
        "name": "ONE",
        "working_dir": str(working_dir),
        "workspace_key": str(Path(workspace_key).expanduser().resolve()),
    }
    record.update(extra)
    return record


def _observe(state_home, workspace_key, working_dir, state, *, age=0.0):
    CheckoutCache.publish(
        state_home,
        workspace_key,
        {
            CheckoutCache.key(working_dir): CheckoutObservation(
                state=state, at=time.time() - age
            )
        },
    )


class TestTheProviderNeverProbes:
    def test_a_value_never_forks_git_even_with_no_cache_at_all(
        self, state_home, workspace, monkeypatch
    ):
        """The gate: zero `git` invocations on a paint path, cache or no cache."""
        import hive_ide.checkout_cache as module

        def refuse(*_args, **_kwargs):
            raise AssertionError("a paint probed a checkout")

        monkeypatch.setattr(module, "inspect_linked_checkout", refuse)
        provider = CheckoutProvider()
        assert provider.value(state_home, _session(workspace, workspace)) is None

    def test_a_stalled_probe_cannot_delay_a_paint(self, state_home, workspace, monkeypatch):
        """One contended checkout used to be able to hold a frame for seconds."""
        import hive_ide.checkout_cache as module

        monkeypatch.setattr(
            module,
            "inspect_linked_checkout",
            lambda *_a, **_k: time.sleep(2) or GitCheckoutStatus("live"),
        )
        provider = CheckoutProvider()
        started = time.monotonic()
        provider.value(state_home, _session(workspace, workspace))
        assert time.monotonic() - started < 0.5


class TestAMissIsNotAnObservation:
    def test_a_miss_does_not_paint_shipped_from_the_records_own_metadata(
        self, state_home, workspace
    ):
        """The defect the cache could have introduced.

        `worktree_ahead=False` was written when the worktree last shipped. Reading it
        on a cache miss would draw `shipped` over a checkout that has moved on since.
        """
        session = _session(workspace, workspace, worktree_ahead=False)
        assert CheckoutProvider().value(state_home, session) is None

    def test_an_expired_observation_is_a_miss_and_not_reused(self, state_home, workspace):
        _observe(state_home, workspace, workspace, "live", age=CheckoutCache.MAX_REUSE_SECONDS + 1)
        session = _session(workspace, workspace, worktree_ahead=False)
        assert CheckoutProvider().value(state_home, session) is None

    def test_an_observed_non_checkout_is_not_a_miss(self, state_home, workspace):
        """An ordinary repo or directory: observed, and not a linked checkout.

        It renders nothing — like a miss — but it must still suppress the record's
        historical merge metadata, which is what it did before the cache existed.
        """
        _observe(state_home, workspace, workspace, None)
        session = _session(workspace, workspace, worktree_ahead=False)
        assert CheckoutProvider().value(state_home, session) is None

    def test_a_failed_probe_is_stored_as_its_own_verdict(self, state_home, workspace):
        _observe(state_home, workspace, workspace, "unknown")
        assert CheckoutProvider().value(state_home, _session(workspace, workspace)) == "unknown"


class TestWhatAFreshObservationDraws:
    def test_inside_the_reuse_age_shipped_is_drawn_as_it_stands(self, state_home, workspace):
        """Simon's decision, 2026-11-25: the last known icon, not a blank column.

        A checkout dirtied a second ago still shows `shipped` until the next refresh
        pass. The icon is evidence of an observation, never a licence to skip
        `git status` before acting.
        """
        _observe(state_home, workspace, workspace, "shipped", age=4.0)
        assert CheckoutProvider().value(state_home, _session(workspace, workspace)) == "shipped"

    def test_a_deleted_directory_is_missing_without_any_probe(self, state_home, tmp_path):
        gone = tmp_path / "gone"
        session = _session(gone, tmp_path)
        assert CheckoutProvider().value(state_home, session) == "missing"

    def test_a_recreated_directory_outranks_a_stale_missing(self, state_home, tmp_path):
        """A `stat` is always current; an observation can be five seconds old."""
        back = tmp_path / "back"
        _observe(state_home, tmp_path, back, "missing")
        back.mkdir()
        assert CheckoutProvider().value(state_home, _session(back, tmp_path)) is None

    def test_a_record_with_no_working_dir_still_uses_its_own_metadata(
        self, state_home, workspace
    ):
        """The legacy path is untouched: two sessions can share a directory and
        still differ, so `busy`/`missing` keep coming from each record."""
        merged = {
            "id": "s2",
            "name": "TWO",
            "workspace_key": str(workspace.resolve()),
            "worktree_merged": True,
            "subagents": {"running": 2},
        }
        assert CheckoutProvider().value(state_home, merged) == "busy"
        merged["subagents"] = {"running": 0}
        assert CheckoutProvider().value(state_home, merged) == "missing"


class TestTheRefresherIsTheOnlyProber:
    def test_one_pass_probes_each_distinct_directory_once(self, state_home, workspace, tmp_path):
        """Two sessions in one worktree share a key, so they share a probe."""
        probed: list[str] = []

        def probe(directory):
            probed.append(str(directory))
            return GitCheckoutStatus("live")

        refresher = CheckoutRefresher(state_home, workspace, probe=probe)
        refresher.note_directories([workspace, workspace, str(workspace) + "/"])
        published = refresher.run_once()
        assert len(probed) == len(published) == 1

    def test_a_pass_is_bounded_so_a_big_workspace_cannot_burst(
        self, state_home, workspace, tmp_path
    ):
        directories = []
        for index in range(CheckoutRefresher.MAX_PROBES_PER_PASS + 5):
            directory = tmp_path / f"w{index}"
            directory.mkdir()
            directories.append(directory)
        probed: list[str] = []
        refresher = CheckoutRefresher(
            state_home,
            workspace,
            probe=lambda d: probed.append(str(d)) or GitCheckoutStatus("live"),
        )
        refresher.note_directories(directories)
        refresher.run_once()
        assert len(probed) == CheckoutRefresher.MAX_PROBES_PER_PASS
        refresher.run_once()
        assert len(probed) == len(directories)      # the rest, next pass

    def test_a_fresh_observation_is_not_probed_again(self, state_home, workspace):
        _observe(state_home, workspace, workspace, "live")
        probed: list[str] = []
        refresher = CheckoutRefresher(
            state_home,
            workspace,
            probe=lambda d: probed.append(str(d)) or GitCheckoutStatus("live"),
        )
        refresher.note_directories([workspace])
        assert refresher.run_once() == {}
        assert probed == []

    def test_a_probe_that_raises_never_escapes_a_pass(self, state_home, workspace):
        def explode(_directory):
            raise RuntimeError("git went missing")

        refresher = CheckoutRefresher(state_home, workspace, probe=explode)
        refresher.note_directories([workspace])
        assert refresher.run_once() == {}

    def test_the_thread_does_not_block_the_caller_and_stops_promptly(
        self, state_home, workspace
    ):
        """It runs beside the render loop, which draws before it reads keys."""
        entered = threading.Event()

        def slow(_directory):
            entered.set()
            time.sleep(1.5)
            return GitCheckoutStatus("live")

        refresher = CheckoutRefresher(state_home, workspace, probe=slow, interval=0.01)
        refresher.note_directories([workspace])
        started = time.monotonic()
        refresher.start()
        assert entered.wait(1.0)
        assert time.monotonic() - started < 0.5      # start() did not wait for the probe
        refresher.stop(timeout=0.1)
        assert time.monotonic() - started < 1.2      # nor did stop()


class TestTheFileIsDisposableAndVersioned:
    def test_the_version_is_in_the_name_so_packages_cannot_misread_each_other(
        self, state_home, workspace
    ):
        assert CheckoutCache.path(state_home, workspace).name == "checkout-cache-v1.json"

    @pytest.mark.parametrize(
        "payload",
        [
            "",
            "{",
            "null",
            "[]",
            '{"version": 999, "checkouts": {}}',
            '{"version": 1}',
            '{"version": 1, "checkouts": []}',
        ],
    )
    def test_every_unusable_document_is_a_miss(self, state_home, workspace, payload):
        target = CheckoutCache.path(state_home, workspace)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(payload, encoding="utf-8")
        assert CheckoutCache.read(state_home, workspace) == {}

    @pytest.mark.parametrize(
        "entry",
        [
            {"state": 7, "at": 0.0},
            {"state": "live"},
            {"state": "live", "at": "soon"},
            {"state": "live", "at": True},
            "live",
        ],
    )
    def test_a_malformed_entry_is_dropped_without_dropping_the_file(
        self, state_home, workspace, entry
    ):
        good = CheckoutCache.key(workspace)
        document = {
            "version": 1,
            "checkouts": {"/bad": entry, good: {"state": "live", "at": time.time()}},
        }
        target = CheckoutCache.path(state_home, workspace)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(document), encoding="utf-8")
        entries = CheckoutCache.read(state_home, workspace)
        assert set(entries) == {good}

    def test_an_unknown_version_is_refused_even_when_its_entries_look_usable(
        self, state_home, workspace
    ):
        """A mutant that dropped the version check survived the first version of
        this file, because the only unknown-version document it was shown had an
        empty `checkouts` — so reading it anyway returned the same `{}`."""
        document = {
            "version": CheckoutCache.VERSION + 1,
            "checkouts": {
                CheckoutCache.key(workspace): {"state": "live", "at": time.time()}
            },
        }
        target = CheckoutCache.path(state_home, workspace)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(document), encoding="utf-8")
        assert CheckoutCache.read(state_home, workspace) == {}
        assert CheckoutProvider().value(state_home, _session(workspace, workspace)) is None

    def test_a_timestamp_from_the_future_is_not_evidence(self, state_home, workspace):
        _observe(state_home, workspace, workspace, "live", age=-60.0)
        assert CheckoutCache.read(state_home, workspace) == {}

    def test_an_unreadable_file_is_a_miss_not_an_exception(self, state_home, workspace):
        target = CheckoutCache.path(state_home, workspace)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.mkdir()      # a directory where the file should be
        assert CheckoutCache.read(state_home, workspace) == {}
        assert CheckoutProvider().value(state_home, _session(workspace, workspace)) is None

    def test_a_publish_keeps_another_writers_entries_and_the_newer_observation(
        self, state_home, workspace, tmp_path
    ):
        other = tmp_path / "other"
        other.mkdir()
        _observe(state_home, workspace, other, "live")
        _observe(state_home, workspace, workspace, "shipped", age=10.0)
        _observe(state_home, workspace, workspace, "live")
        entries = CheckoutCache.read(state_home, workspace)
        assert set(entries) == {CheckoutCache.key(other), CheckoutCache.key(workspace)}
        assert entries[CheckoutCache.key(workspace)].state == "live"

    def test_an_older_observation_never_overwrites_a_newer_one(self, state_home, workspace):
        _observe(state_home, workspace, workspace, "live")
        _observe(state_home, workspace, workspace, "shipped", age=10.0)
        entries = CheckoutCache.read(state_home, workspace)
        assert entries[CheckoutCache.key(workspace)].state == "live"

    def test_a_temporary_file_left_by_a_killed_writer_is_ignored(self, state_home, workspace):
        _observe(state_home, workspace, workspace, "live")
        target = CheckoutCache.path(state_home, workspace)
        (target.parent / f".{target.stem}.orphan.tmp").write_text("{", encoding="utf-8")
        assert CheckoutProvider().value(state_home, _session(workspace, workspace)) == "live"

    def test_the_file_is_bounded(self, state_home, workspace):
        observations = {
            f"/w{index}": CheckoutObservation(state="live", at=time.time() - index)
            for index in range(CheckoutCache.MAX_ENTRIES + 20)
        }
        CheckoutCache.publish(state_home, workspace, observations)
        assert len(CheckoutCache.read(state_home, workspace)) == CheckoutCache.MAX_ENTRIES

    def test_two_workspaces_keep_separate_files(self, state_home, tmp_path):
        first, second = tmp_path / "one", tmp_path / "two"
        first.mkdir()
        second.mkdir()
        _observe(state_home, first, first, "live")
        assert CheckoutCache.read(state_home, first)
        assert CheckoutCache.read(state_home, second) == {}


class TestTheReaderRefreshesWhenTheFileChanges:
    def test_a_long_lived_reader_sees_a_refreshers_publish(self, state_home, workspace):
        """The render loop's provider instance lives for thousands of frames, so it
        has to notice the refresher rewriting the file UNDER it.

        The first read must be of a file that already exists: a mutant that only
        ever read a location it had not seen before survived the earlier version of
        this test, because the absent first read recorded no stamp to go stale.
        """
        _observe(state_home, workspace, workspace, "shipped")
        reader = CheckoutCacheReader()
        first = reader.fresh(state_home, workspace, workspace)
        assert first is not None and first.state == "shipped"
        time.sleep(0.01)      # a measurable mtime step on a coarse filesystem
        _observe(state_home, workspace, workspace, "live")
        second = reader.fresh(state_home, workspace, workspace)
        assert second is not None and second.state == "live"

    def test_a_reader_re_reads_nothing_while_the_file_is_unchanged(
        self, state_home, workspace, monkeypatch
    ):
        """The other half: one `stat` per paint, not one parse per row."""
        _observe(state_home, workspace, workspace, "live")
        reader = CheckoutCacheReader()
        reader.entries(state_home, workspace)
        reads: list[str] = []
        original = CheckoutCache.read

        def counting(*args, **kwargs):
            reads.append("read")
            return original(*args, **kwargs)

        monkeypatch.setattr(CheckoutCache, "read", counting)
        for _ in range(5):
            reader.entries(state_home, workspace)
        assert reads == []

    def test_a_reader_survives_the_file_disappearing(self, state_home, workspace):
        reader = CheckoutCacheReader()
        _observe(state_home, workspace, workspace, "live")
        assert reader.fresh(state_home, workspace, workspace) is not None
        CheckoutCache.path(state_home, workspace).unlink()
        assert reader.fresh(state_home, workspace, workspace) is None
