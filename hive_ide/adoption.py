"""Adopt existing agent-local conversations into hive-ide sessions."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import UsageError
from .store import StateStore


@dataclass(frozen=True)
class AdoptableConversation:
    driver_id: str
    reference: str
    label: str
    working_dir: str
    updated_at: str | None
    source_path: str


class ClaudeSessionAdopter:
    """Metadata-only scanner for Claude Code's directory-scoped JSONL sessions."""

    def __init__(self, *, home: Path | None = None):
        self.home = home or Path.home()

    def conversations(self, working_dir: str) -> list[AdoptableConversation]:
        directory = self._project_dir(working_dir)
        if not directory.is_dir():
            return []
        conversations = [
            conversation
            for path in directory.glob("*.jsonl")
            if (conversation := self._conversation(path, working_dir)) is not None
        ]
        conversations.sort(key=lambda item: item.updated_at or "", reverse=True)
        return conversations

    def _project_dir(self, working_dir: str) -> Path:
        resolved = Path(working_dir).expanduser().resolve()
        encoded = "-" + "-".join(resolved.parts[1:])
        return self.home / ".claude" / "projects" / encoded

    def _conversation(
        self, path: Path, working_dir: str
    ) -> AdoptableConversation | None:
        reference = path.stem
        updated_at: str | None = None
        try:
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(payload.get("sessionId"), str):
                        reference = payload["sessionId"]
                    if isinstance(payload.get("timestamp"), str):
                        updated_at = payload["timestamp"]
        except OSError:
            return None
        return AdoptableConversation(
            driver_id="claude",
            reference=reference,
            label=f"CLAUDE {reference[:8]}",
            working_dir=str(Path(working_dir).expanduser().resolve()),
            updated_at=updated_at,
            source_path=str(path),
        )


class ConversationAdopter:
    def __init__(self, store: StateStore, config: dict[str, Any]):
        self.store = store
        self.config = config

    def available(self, *, driver_id: str, working_dir: str) -> list[AdoptableConversation]:
        if driver_id != "claude":
            raise UsageError(
                "Only Claude adoption is supported right now. Use --driver=claude."
            )
        return ClaudeSessionAdopter().conversations(working_dir)

    def existing_references(self, *, driver_id: str) -> set[str]:
        references: set[str] = set()
        for collection in ("sessions", "archive"):
            for record in self.store.list(collection):
                driver = record.get("driver") or {}
                resume = driver.get("resume") or {}
                reference = resume.get("reference")
                if driver.get("id") == driver_id and isinstance(reference, str):
                    references.add(reference)
        return references
