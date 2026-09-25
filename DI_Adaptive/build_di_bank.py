"""
DI_Adaptive/build_di_bank.py — GMAT Focus Edition Data Insights Question Bank Builder

Unlike Quant's single combined CSV, GMAT Club's DI directory was scraped as 4
separate per-category exports (DS.csv, MSR.csv, TPA.csv, G&T.csv), each with
its own (slightly inconsistent) column layout from Instant Data Scraper:

    - URL is in a column containing "href" (e.g. "ttl href").
    - Difficulty is in the "d" column (e.g. "605-655", "805+", "Sub 505").
    - For G&T.csv only, the subtopic ("Tables" or "Graphs") is in "tsub 2";
      the other 3 files don't need a sub-quota-relevant subtopic split.

Since each file IS the category, no keyword-based tag classification is
needed - the category comes straight from which file a row is in.

Writes:
    - questions_di_v1.json      (the combined question bank, per architecture §2)
    - gmatclub_clean_di.csv     (cleaned/normalized rows, all categories combined)

Usage:
    python build_di_bank.py [--ds DS.csv] [--msr MSR.csv] [--tpa TPA.csv] [--gt "G&T.csv"]
                             [--out-json questions_di_v1.json] [--out-csv gmatclub_clean_di.csv]
                             [--msr-child-count 3]
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Constants (architecture §5 — IRT Parameters & Band Mapping)
# ---------------------------------------------------------------------------

BAND_INFO = {
    1: {"label": "Very Easy", "anchor": -1.60},
    2: {"label": "Easy", "anchor": -0.90},
    3: {"label": "Easy-Medium", "anchor": -0.60},
    4: {"label": "Medium", "anchor": -0.25},
    5: {"label": "Hard", "anchor": 0.15},
    6: {"label": "Very Hard", "anchor": 0.42},
    7: {"label": "Extreme", "anchor": 1.00},
    8: {"label": "Abnormally Hard", "anchor": 1.50},
}

# Raw GMAT Club difficulty tag -> band index (architecture §2, difficulty label table)
DIFFICULTY_TAG_PATTERNS: list[tuple[re.Pattern, int]] = [
    (re.compile(r"\bsub[\s\-]*505\b", re.IGNORECASE), 1),
    (re.compile(r"\b505[\s\-]*555\b"), 2),
    (re.compile(r"\b555[\s\-]*605\b"), 3),
    (re.compile(r"\b605[\s\-]*655\b"), 4),
    (re.compile(r"\b655[\s\-]*705\b"), 5),
    (re.compile(r"\b705[\s\-]*805\b"), 6),
    (re.compile(r"\b805\s*\+"), 7),
]

MSR_CATEGORY = "MSRs"
GRAPHS_AND_TABLES_CATEGORY = "Graphs and Tables"

# One input file = one fixed category (architecture §3 — DI category table).
CATEGORY_FILES: dict[str, str] = {
    "ds": "Data Sufficiency",
    "msr": MSR_CATEGORY,
    "tpa": "Two-Part Analysis",
    "gt": GRAPHS_AND_TABLES_CATEGORY,
}

# G&T.csv's "tsub 2" values map directly to the official subtopic names.
GT_SUBTOPIC_MAP = {
    "tables": "Table Analysis",
    "graphs": "Graphics Interpretation",
}

URL_COLUMN_HINTS = ("href", "url", "link")
DIFFICULTY_COLUMN_HINTS = ("d",)
SUBTOPIC_COLUMN_HINTS = ("tsub 2", "tsub2")

NON_QUESTION_URL_PATTERNS = re.compile(
    r"/forum-\d+\.html$|/(gmat-club|welcome|announcements?)-", re.IGNORECASE
)


# ---------------------------------------------------------------------------
# Column detection
# ---------------------------------------------------------------------------

def detect_column(fieldnames: list[str], hints: tuple[str, ...]) -> Optional[str]:
    for hint in hints:
        for name in fieldnames:
            if name.strip().lower() == hint:
                return name
    for hint in hints:
        for name in fieldnames:
            if hint in name.lower():
                return name
    return None


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def parse_difficulty(text: str) -> Optional[int]:
    text = (text or "").strip()
    for pattern, band_index in DIFFICULTY_TAG_PATTERNS:
        if pattern.search(text):
            return band_index
    return None


def normalize_url(url: str) -> str:
    url = url.strip()
    url = url.split("?", 1)[0].split("#", 1)[0]
    return url.rstrip("/")


def make_id(url: str) -> str:
    return "q-" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]


def is_question_row(url: str) -> bool:
    if not url or not url.lower().startswith("http"):
        return False
    if NON_QUESTION_URL_PATTERNS.search(url):
        return False
    return True


# ---------------------------------------------------------------------------
# Per-file processing
# ---------------------------------------------------------------------------

def process_category_file(
    input_csv: Path,
    category: str,
    msr_child_count: int,
    seen_urls: set[str],
) -> tuple[list[dict], list[dict], dict[str, int]]:
    """Parse one category's CSV export. Returns (questions, clean_rows, skip_counters)."""
    counters = {"non_question": 0, "no_difficulty": 0, "duplicate": 0}
    questions: list[dict] = []
    clean_rows: list[dict] = []

    if not input_csv.exists():
        print(f"  Skipping {category}: file not found ({input_csv})", file=sys.stderr)
        return questions, clean_rows, counters

    with input_csv.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            print(f"  Skipping {category}: {input_csv} has no header row.", file=sys.stderr)
            return questions, clean_rows, counters

        fieldnames = list(reader.fieldnames)
        url_col = detect_column(fieldnames, URL_COLUMN_HINTS)
        difficulty_col = detect_column(fieldnames, DIFFICULTY_COLUMN_HINTS)
        subtopic_col = detect_column(fieldnames, SUBTOPIC_COLUMN_HINTS)

        if url_col is None or difficulty_col is None:
            print(
                f"  Skipping {category}: could not detect URL/difficulty columns "
                f"among headers {fieldnames}", file=sys.stderr,
            )
            return questions, clean_rows, counters

        print(f"  {category}: URL column '{url_col}', difficulty column '{difficulty_col}'"
              + (f", subtopic column '{subtopic_col}'" if subtopic_col else ""))

        is_msr = category == MSR_CATEGORY
        is_gt = category == GRAPHS_AND_TABLES_CATEGORY

        for row in reader:
            raw_url = (row.get(url_col) or "").strip()
            if not is_question_row(raw_url):
                counters["non_question"] += 1
                continue

            url = normalize_url(raw_url)
            if url in seen_urls:
                counters["duplicate"] += 1
                continue

            band_index = parse_difficulty(row.get(difficulty_col))
            if band_index is None:
                counters["no_difficulty"] += 1
                continue

            if is_gt:
                raw_subtopic = (row.get(subtopic_col) or "").strip().lower()
                subtopic = GT_SUBTOPIC_MAP.get(raw_subtopic, "Table Analysis")
            else:
                subtopic = category

            band_data = BAND_INFO[band_index]
            qid = make_id(url)

            question = {
                "id": qid,
                "url": url,
                "difficulty_band": band_data["label"],
                "difficulty_index": band_index,
                "difficulty_anchor": band_data["anchor"],
                "category": category,
                "subtopic": subtopic,
                "delivery_type": "MSR" if is_msr else "single",
                "active": True,
            }
            if is_msr:
                # GMAT Club tags an MSR thread with one difficulty label covering
                # the whole prompt - all sub-questions default to that difficulty
                # unless a future data source supplies per-child bands.
                question["msr_child_count"] = msr_child_count
                question["msr_child_difficulty_indices"] = [band_index] * msr_child_count

            questions.append(question)
            seen_urls.add(url)

            clean_rows.append({
                "id": qid,
                "url": url,
                "difficulty_band": band_data["label"],
                "difficulty_index": band_index,
                "category": category,
                "subtopic": subtopic,
                "delivery_type": question["delivery_type"],
            })

    return questions, clean_rows, counters


# ---------------------------------------------------------------------------
# Main build routine
# ---------------------------------------------------------------------------

def build_bank(inputs: dict[str, Path], out_json: Path, out_csv: Path, msr_child_count: int) -> None:
    seen_urls: set[str] = set()
    all_questions: list[dict] = []
    all_clean_rows: list[dict] = []
    totals = {"non_question": 0, "no_difficulty": 0, "duplicate": 0}

    print("Processing category files:")
    for key, category in CATEGORY_FILES.items():
        questions, clean_rows, counters = process_category_file(
            inputs[key], category, msr_child_count, seen_urls
        )
        all_questions.extend(questions)
        all_clean_rows.extend(clean_rows)
        for k in totals:
            totals[k] += counters[k]
        print(f"    -> kept {len(questions)} question(s)")

    out_json.write_text(json.dumps(all_questions, indent=2), encoding="utf-8")

    with out_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["id", "url", "difficulty_band", "difficulty_index",
                        "category", "subtopic", "delivery_type"],
        )
        writer.writeheader()
        writer.writerows(all_clean_rows)

    print_stats(all_questions)
    print(f"\n  Skipped (non-question row): {totals['non_question']}")
    print(f"  Skipped (no difficulty tag): {totals['no_difficulty']}")
    print(f"  Skipped (duplicate URL):     {totals['duplicate']}")
    print(f"\nWrote {len(all_questions)} questions to {out_json}")
    print(f"Wrote cleaned CSV to {out_csv}")


def print_stats(questions: list[dict]) -> None:
    band_counts = Counter(q["difficulty_index"] for q in questions)
    category_counts = Counter(q["category"] for q in questions)
    subtopic_counts = Counter((q["category"], q["subtopic"]) for q in questions)
    msr_count = sum(1 for q in questions if q["delivery_type"] == "MSR")

    print("\n--- Band Distribution ---")
    for band_index in sorted(BAND_INFO):
        label = BAND_INFO[band_index]["label"]
        count = band_counts.get(band_index, 0)
        print(f"  Band {band_index} ({label}): {count}")

    print("\n--- Category Distribution ---")
    for category, count in category_counts.most_common():
        print(f"  {category}: {count}")

    print("\n--- Subtopic Distribution ---")
    for (category, subtopic), count in sorted(subtopic_counts.items()):
        print(f"  {category} / {subtopic}: {count}")

    print("\n--- Delivery Type ---")
    print(f"  MSR prompts: {msr_count}")
    print(f"  Single questions: {len(questions) - msr_count}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build the GMAT Data Insights question bank from 4 per-category GMAT Club CSV exports."
    )
    parser.add_argument("--ds", default="DS.csv", help="Data Sufficiency CSV (default: DS.csv)")
    parser.add_argument("--msr", default="MSR.csv", help="MSR CSV (default: MSR.csv)")
    parser.add_argument("--tpa", default="TPA.csv", help="Two-Part Analysis CSV (default: TPA.csv)")
    parser.add_argument("--gt", default="G&T.csv", help="Graphs and Tables CSV (default: G&T.csv)")
    parser.add_argument("--out-json", default="questions_di_v1.json", help="Output JSON bank path")
    parser.add_argument("--out-csv", default="gmatclub_clean_di.csv", help="Output cleaned CSV path")
    parser.add_argument("--msr-child-count", type=int, default=3,
                         help="Number of sub-questions per MSR prompt (default: 3, per architecture §11)")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    inputs = {"ds": Path(args.ds), "msr": Path(args.msr), "tpa": Path(args.tpa), "gt": Path(args.gt)}
    build_bank(inputs, Path(args.out_json), Path(args.out_csv), args.msr_child_count)
