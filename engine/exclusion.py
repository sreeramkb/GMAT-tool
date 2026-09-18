"""
engine/exclusion.py — URL normalization, deduplication, and used-question
ledger management (architecture §16.2).

The ledger is a flat JSON list on disk tracking every question ever served,
so future sessions can build exclusion sets and avoid repeats.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional, Set
from urllib.parse import urlsplit, urlunsplit


def normalize_url(url: str) -> str:
    """Strip query params, fragments, and trailing slashes so duplicates are caught."""
    if not url:
        return url
    parts = urlsplit(url.strip())
    normalized = urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))
    return normalized


class UsedQuestionLedger:
    """Append-only JSON ledger of every question ever served across all sessions."""

    def __init__(self, ledger_path: Path):
        self.ledger_path = Path(ledger_path)
        self._entries: List[dict] = self._load()

    def _load(self) -> List[dict]:
        if not self.ledger_path.exists():
            return []
        try:
            return json.loads(self.ledger_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []

    def _save(self) -> None:
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        self.ledger_path.write_text(json.dumps(self._entries, indent=2), encoding="utf-8")

    def used_urls(self) -> Set[str]:
        return {normalize_url(entry["url"]) for entry in self._entries}

    def used_ids(self) -> Set[str]:
        return {entry["id"] for entry in self._entries if "id" in entry}

    def record(self, question_id: str, url: str, session_id: str) -> None:
        self._entries.append({
            "id": question_id,
            "url": normalize_url(url),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "session_id": session_id,
        })
        self._save()

    def record_many(self, questions: Iterable[dict], session_id: str) -> None:
        for q in questions:
            self._entries.append({
                "id": q.get("id"),
                "url": normalize_url(q.get("url", "")),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "session_id": session_id,
            })
        self._save()


def build_exclusion_set(
    ledger: UsedQuestionLedger,
    session_served_ids: Optional[Iterable[str]] = None,
) -> Set[str]:
    """Combine the on-disk ledger with the current in-progress session's served IDs."""
    excluded = ledger.used_ids()
    if session_served_ids:
        excluded |= set(session_served_ids)
    return excluded


def filter_available_questions(bank: List[dict], excluded_ids: Set[str]) -> List[dict]:
    return [q for q in bank if q.get("id") not in excluded_ids and q.get("active", True)]
