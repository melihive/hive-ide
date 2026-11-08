from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {".json", ".md", ".py", ".toml", ".yaml", ".yml"}
SKIP_PARTS = {
    ".git",
    ".pytest_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "temp",
}
# This repo's own git worktrees live in `worktree/<name>`: other branches'
# checkouts, which no source distribution ships. Scanning them reports
# violations that cannot reach a user, and they fail on whatever those branches
# have not caught up to yet — invisible from inside a worktree, where the
# directory does not exist, which is exactly where a pre-ship verify runs.
#
# Only the TOP-LEVEL directory is skipped. A package module named `worktree`
# must still be scanned: this package manages worktrees, so that name is a
# plausible future module, and a blanket part-name skip would go blind on it.
SKIP_TOP_LEVEL = {"worktree"}


def is_scannable(path: Path, root: Path) -> bool:
    """True when `path` is part of the tree a source distribution ships."""
    parts = path.relative_to(root).parts
    if parts and parts[0] in SKIP_TOP_LEVEL:
        return False
    return not any(
        part in SKIP_PARTS
        or part.startswith(".venv")
        or part.endswith(".egg-info")
        for part in parts
    )

# Assemble private terms without placing those terms in the export tree verbatim.
PRIVATE_WORDS = (
    "colo" + "ny",
    "colo" + "nies",
    "bee" + "keeper",
    "bro" + "ker",
    "s" + "op",
    "gr" + "ant",
    "dc" + "er",
    "dc" + "ers",
)
PRIVATE_PHRASES = (
    "dynamite" + " circle",
    "dynamite" + " jobs",
    "hive" + " core",
    "hive" + " runtime",
    "dc" + "-team",
    "dc" + "-web",
    "dc" + "-mobile",
    "dj" + "-web",
    "dynamite" + "-ventures",
    "hive" + "-tech-support",
    "HIVE" + "_SCOPE_",
)
# Fleet machine names. A docstring that names the host an incident happened on
# reads as internal infrastructure to every reader outside the fleet, and the word
# list above never covered that class — two such names shipped in 1.0.96's source
# and the check stayed green. Originally scoped to the package tree because test
# fixtures and changelog history still carried older host names; that scrub is
# done (2026-11-08) and the guard now covers the WHOLE exported tree, source and
# tests and changelog alike. A sdist ships the tests, so a fixture naming a real
# machine is published exactly like a docstring that does.
#
# These stay assembled from fragments for the same reason the words above do: the
# guard scans its own file, and a verbatim list here would be the violation it
# exists to catch.
PRIVATE_HOSTS = (
    "vi" + "vo",
    "g" + "pd",
    "m" + "im",
    "z" + "en",
)
HOST_PATTERN = re.compile(
    r"\b(?:" + "|".join(re.escape(host) for host in PRIVATE_HOSTS) + r")\b",
    re.IGNORECASE,
)
WORD_PATTERN = re.compile(
    r"\b(?:" + "|".join(re.escape(term) for term in PRIVATE_WORDS) + r")\b",
    re.IGNORECASE,
)


def vocabulary_violations(root: Path) -> list[str]:
    violations: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in TEXT_SUFFIXES:
            continue
        if not is_scannable(path, root):
            continue
        text = path.read_text(encoding="utf-8")
        for line_number, line in enumerate(text.splitlines(), 1):
            if WORD_PATTERN.search(line) or any(
                phrase.lower() in line.lower() for phrase in PRIVATE_PHRASES
            ):
                violations.append(f"{path.relative_to(root)}:{line_number}")
    return violations


def test_public_tree_uses_public_vocabulary():
    assert vocabulary_violations(ROOT) == []


def test_vocabulary_check_observes_a_real_violation(tmp_path):
    bad = tmp_path / "example.md"
    bad.write_text(f"private term: {PRIVATE_WORDS[0]}\n", encoding="utf-8")
    assert vocabulary_violations(tmp_path) == ["example.md:1"]


def test_pane_lease_commands_are_public():
    from hive_ide.cli import build_parser

    help_text = build_parser().format_help()
    for command in ("pane-lease", "pane-release", "pane-status", "capabilities"):
        assert command in help_text


HOST_SUFFIXES = {".md", ".py"}


def host_violations(root: Path) -> list[str]:
    violations: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in HOST_SUFFIXES:
            continue
        if not is_scannable(path, root):
            continue
        text = path.read_text(encoding="utf-8")
        for line_number, line in enumerate(text.splitlines(), 1):
            if HOST_PATTERN.search(line):
                violations.append(f"{path.relative_to(root)}:{line_number}")
    return violations


def test_exported_tree_names_no_fleet_host():
    assert host_violations(ROOT) == []


def test_host_check_observes_a_real_violation(tmp_path):
    bad = tmp_path / "frame.py"
    bad.write_text(f"# the freeze was seen on {PRIVATE_HOSTS[2]}\n", encoding="utf-8")
    assert host_violations(tmp_path) == ["frame.py:1"]


def test_scan_skips_the_repos_own_worktrees_but_not_a_worktree_module(tmp_path):
    """A sibling checkout is not shipped; a package module of that name is.

    Run from the main checkout, the scan walked `worktree/<name>` — other
    branches, which still carry whatever this branch just scrubbed — so the
    guard failed on files no source distribution contains. A pre-ship verify
    runs INSIDE a worktree, where that directory does not exist, so it could
    never see this.
    """
    sibling = tmp_path / "worktree" / "other-branch" / "hive_ide"
    sibling.mkdir(parents=True)
    (sibling / "frame.py").write_text(
        f"# seen on {PRIVATE_HOSTS[0]}\n", encoding="utf-8")
    assert host_violations(tmp_path) == [], (
        "the scan walked the repo's own worktrees, so another branch's "
        "un-scrubbed file fails this branch's guard")

    # ...but the same name INSIDE the package is shipped, so it is still read.
    module = tmp_path / "hive_ide" / "worktree"
    module.mkdir(parents=True)
    (module / "manage.py").write_text(
        f"# seen on {PRIVATE_HOSTS[0]}\n", encoding="utf-8")
    assert host_violations(tmp_path) == ["hive_ide/worktree/manage.py:1"], (
        "a package module named `worktree` was skipped — the guard is blind "
        "exactly where this package is most likely to add one")


def test_host_check_now_reaches_markdown_and_tests(tmp_path):
    """The widening is the point — pin the two file classes it just gained.

    Scoped to `*.py` under `hive_ide/`, the guard was blind to both places the
    remaining host names actually lived: a changelog entry and a test fixture.
    Without this, narrowing the scope back to the package tree would leave every
    other test here green.
    """
    (tmp_path / "CHANGELOG.md").write_text(
        f"- fixed on {PRIVATE_HOSTS[0]}\n", encoding="utf-8"
    )
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_thing.py").write_text(
        f'HOST = "{PRIVATE_HOSTS[1]}"\n', encoding="utf-8"
    )
    assert host_violations(tmp_path) == ["CHANGELOG.md:1", "tests/test_thing.py:1"]
