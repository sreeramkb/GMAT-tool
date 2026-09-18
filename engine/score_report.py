"""
engine/score_report.py — append-only CSV logic for the master score report
(architecture §16.3).

Every completed session appends one row. Complex fields (theta trajectory,
accuracy-by-band, accuracy-by-category, diagnostics) are stored as JSON
strings inside their CSV cell so the file stays a single flat table.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

MASTER_REPORT_COLUMNS: List[str] = [
    "session_id",
    "timestamp",
    "section",
    "engine_mode",
    "scaled_score",
    "accuracy",
    "time_used_seconds",
    "theta_trajectory",
    "accuracy_by_band",
    "accuracy_by_category",
    "diagnostics",
]

_JSON_COLUMNS = {"theta_trajectory", "accuracy_by_band", "accuracy_by_category", "diagnostics"}


def _serialize_row(row: Dict[str, Any]) -> Dict[str, Any]:
    serialized = {}
    for column in MASTER_REPORT_COLUMNS:
        value = row.get(column, "")
        if isinstance(value, (dict, list)):
            value = json.dumps(value)
        serialized[column] = value
    return serialized


def append_score_report(
    csv_path: Path,
    session_id: str,
    section: str,
    engine_mode: str,
    scaled_score: int,
    accuracy: float,
    time_used_seconds: float,
    theta_trajectory: List[float],
    accuracy_by_band: Dict[str, float],
    accuracy_by_category: Dict[str, float],
    diagnostics: Dict[str, Any],
    timestamp: Optional[str] = None,
) -> None:
    """Append one row to the master score report CSV, creating it with a header if needed."""
    csv_path = Path(csv_path)
    csv_path.parent.mkdir(parents=True, exist_ok=True)

    row = _serialize_row({
        "session_id": session_id,
        "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
        "section": section,
        "engine_mode": engine_mode,
        "scaled_score": scaled_score,
        "accuracy": accuracy,
        "time_used_seconds": time_used_seconds,
        "theta_trajectory": theta_trajectory,
        "accuracy_by_band": accuracy_by_band,
        "accuracy_by_category": accuracy_by_category,
        "diagnostics": diagnostics,
    })

    file_exists = csv_path.exists() and csv_path.stat().st_size > 0
    with csv_path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=MASTER_REPORT_COLUMNS)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)


def read_all_reports(csv_path: Path) -> List[Dict[str, Any]]:
    """Read the master report CSV back, deserializing the JSON-encoded columns."""
    csv_path = Path(csv_path)
    if not csv_path.exists():
        return []

    rows: List[Dict[str, Any]] = []
    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for raw_row in reader:
            row = dict(raw_row)
            for column in _JSON_COLUMNS:
                if row.get(column):
                    try:
                        row[column] = json.loads(row[column])
                    except json.JSONDecodeError:
                        pass
            rows.append(row)
    return rows
