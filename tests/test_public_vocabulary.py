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
# and the check stayed green. Scoped to the package tree on purpose: test fixtures
# and changelog history still carry older host names, and widening to them is a
# larger scrub than this guard is here to hold.
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
        if any(
            part in SKIP_PARTS
            or part.startswith(".venv")
            or part.endswith(".egg-info")
            for part in path.parts
        ):
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


def host_violations(root: Path) -> list[str]:
    violations: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if any(
            part in SKIP_PARTS or part.startswith(".venv") for part in path.parts
        ):
            continue
        text = path.read_text(encoding="utf-8")
        for line_number, line in enumerate(text.splitlines(), 1):
            if HOST_PATTERN.search(line):
                violations.append(f"{path.relative_to(root)}:{line_number}")
    return violations


def test_package_source_names_no_fleet_host():
    assert host_violations(ROOT / "hive_ide") == []


def test_host_check_observes_a_real_violation(tmp_path):
    bad = tmp_path / "frame.py"
    bad.write_text(f"# the freeze was seen on {PRIVATE_HOSTS[2]}\n", encoding="utf-8")
    assert host_violations(tmp_path) == ["frame.py:1"]
