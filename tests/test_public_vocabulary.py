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
