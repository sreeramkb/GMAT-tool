"""
build_quant_bank.py — GMAT Focus Edition Quant Question Bank Builder

Reads a raw CSV scraped from GMAT Club (via the Instant Data Scraper Chrome
extension), auto-detects the URL/tags columns, classifies each question into
a 1-8 difficulty band and one of the 4 Quant categories/subtopics, generates
stable IDs, and writes:

    - questions_quant_v1.json   (the question bank, per architecture §2)
    - gmatclub_clean_quant.csv  (cleaned/normalized rows)

Usage:
    python build_quant_bank.py [--input gmatclub.csv] [--out-json questions_quant_v1.json]
                                [--out-csv gmatclub_clean_quant.csv]
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

# Category/subtopic keyword mapping (architecture §3 — Quant category table)
# Order matters: first matching rule wins.
CATEGORY_RULES: list[tuple[re.Pattern, str, str]] = [
    (re.compile(r"probabilit|permutation|combinat", re.IGNORECASE),
     "Counting/Sets/Series/Prob/Stats", "Counting & Probability"),
    (re.compile(r"overlapping\s*sets|venn", re.IGNORECASE),
     "Counting/Sets/Series/Prob/Stats", "Overlapping Sets"),
    (re.compile(r"sequence|series|progression", re.IGNORECASE),
     "Counting/Sets/Series/Prob/Stats", "Sequences & Series"),
    (re.compile(r"statistic|\bmean\b|median|standard\s*deviation", re.IGNORECASE),
     "Counting/Sets/Series/Prob/Stats", "Basic Statistics"),
    (re.compile(r"distance|speed|\brate\b|\bwork\b|mixture", re.IGNORECASE),
     "Rates/Ratio/Percent", "Rates & Work"),
    (re.compile(r"\bratio\b|proportion|fraction", re.IGNORECASE),
     "Rates/Ratio/Percent", "Ratios & Proportions"),
    (re.compile(r"percent|interest|profit|discount", re.IGNORECASE),
     "Rates/Ratio/Percent", "Percentages"),
    (re.compile(r"inequalit", re.IGNORECASE),
     "Equal/Unequal/ALG", "Inequalities"),
    (re.compile(r"equation|linear|quadratic", re.IGNORECASE),
     "Equal/Unequal/ALG", "Equalities"),
    (re.compile(r"algebra|absolute\s*value|coordinate\s*geometry", re.IGNORECASE),
     "Equal/Unequal/ALG", "Algebraic Manipulation"),
    (re.compile(r"factor|multiple|\blcm\b|\bgcd\b|prime", re.IGNORECASE),
     "Value/Order/Factors", "Factors & Multiples"),
    (re.compile(r"number\s*propert|remainder|exponent|\broot\b", re.IGNORECASE),
     "Value/Order/Factors", "Value & Number Properties"),
]

DEFAULT_CATEGORY = "Value/Order/Factors"
DEFAULT_SUBTOPIC = "Value & Number Properties"

# Column name hints for auto-detection
URL_COLUMN_HINTS = ("topic-link", "href", "url", "link")
TAG_COLUMN_HINTS = ("tags", "tag")

# Rows that look like directory/overview pages rather than actual questions
NON_QUESTION_URL_PATTERNS = re.compile(
    r"/forum-\d+\.html$|/(gmat-club|welcome|announcements?)-", re.IGNORECASE
)


# ---------------------------------------------------------------------------
# Column detection
# ---------------------------------------------------------------------------

def detect_column(fieldnames: list[str], hints: tuple[str, ...]) -> Optional[str]:
    for hint in hints:
        for name in fieldnames:
            if hint in name.lower():
                return name
    return None


def detect_columns(fieldnames: list[str]) -> tuple[str, str]:
    url_col = detect_column(fieldnames, URL_COLUMN_HINTS)
    tag_col = detect_column(fieldnames, TAG_COLUMN_HINTS)
    if url_col is None:
        raise ValueError(
            f"Could not auto-detect a URL column among headers: {fieldnames}"
        )
    if tag_col is None:
        raise ValueError(
            f"Could not auto-detect a tags column among headers: {fieldnames}"
        )
    return url_col, tag_col


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _cell_texts(value: str | dict[str, str]) -> list[str]:
    if isinstance(value, dict):
        return [str(v or "").strip() for v in value.values() if str(v or "").strip()]
    text = str(value or "").strip()
    return [text] if text else []


def parse_difficulty(tag_text: str | dict[str, str]) -> Optional[int]:
    for text in _cell_texts(tag_text):
        for pattern, band_index in DIFFICULTY_TAG_PATTERNS:
            if pattern.search(text):
                return band_index
    return None


def classify_category(tag_text: str | dict[str, str]) -> tuple[str, str]:
    for text in _cell_texts(tag_text):
        for pattern, category, subtopic in CATEGORY_RULES:
            if pattern.search(text):
                return category, subtopic
    return DEFAULT_CATEGORY, DEFAULT_SUBTOPIC


def normalize_url(url: str) -> str:
    url = url.strip()
    # strip query params / fragments, trailing slash
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
# Main build routine
# ---------------------------------------------------------------------------

def build_bank(input_csv: Path, out_json: Path, out_csv: Path) -> None:
    if not input_csv.exists():
        print(f"Error: input file not found: {input_csv}", file=sys.stderr)
        sys.exit(1)

    with input_csv.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            print("Error: input CSV has no header row.", file=sys.stderr)
            sys.exit(1)
        url_col, tag_col = detect_columns(list(reader.fieldnames))
        print(f"Detected URL column: '{url_col}'")
        print(f"Detected tags column: '{tag_col}'")

        rows = list(reader)

    seen_urls: set[str] = set()
    questions: list[dict] = []
    clean_rows: list[dict] = []

    skipped_non_question = 0
    skipped_no_difficulty = 0
    skipped_duplicate = 0

    for row in rows:
        raw_url = (row.get(url_col) or "").strip()
        tag_text = row

        if not is_question_row(raw_url):
            skipped_non_question += 1
            continue

        url = normalize_url(raw_url)
        if url in seen_urls:
            skipped_duplicate += 1
            continue

        band_index = parse_difficulty(tag_text)
        if band_index is None:
            skipped_no_difficulty += 1
            continue

        category, subtopic = classify_category(tag_text)
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
            "delivery_type": "single",
            "active": True,
        }
        questions.append(question)
        seen_urls.add(url)

        clean_rows.append({
            "id": qid,
            "url": url,
            "tags": tag_text,
            "difficulty_band": band_data["label"],
            "difficulty_index": band_index,
            "category": category,
            "subtopic": subtopic,
        })

    out_json.write_text(json.dumps(questions, indent=2), encoding="utf-8")

    with out_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["id", "url", "tags", "difficulty_band", "difficulty_index",
                        "category", "subtopic"],
        )
        writer.writeheader()
        writer.writerows(clean_rows)

    print_stats(questions)
    print(f"\nParsed rows: {len(rows)}")
    print(f"  Kept:                {len(questions)}")
    print(f"  Skipped (non-question row): {skipped_non_question}")
    print(f"  Skipped (no difficulty tag): {skipped_no_difficulty}")
    print(f"  Skipped (duplicate URL):     {skipped_duplicate}")
    print(f"\nWrote {len(questions)} questions to {out_json}")
    print(f"Wrote cleaned CSV to {out_csv}")


def print_stats(questions: list[dict]) -> None:
    band_counts = Counter(q["difficulty_index"] for q in questions)
    category_counts = Counter(q["category"] for q in questions)
    subtopic_counts = Counter((q["category"], q["subtopic"]) for q in questions)

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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the GMAT Quant question bank from a GMAT Club CSV export.")
    parser.add_argument("--input", default="gmatclub.csv", help="Path to the raw scraped CSV (default: gmatclub.csv)")
    parser.add_argument("--out-json", default="questions_quant_v1.json", help="Output JSON bank path")
    parser.add_argument("--out-csv", default="gmatclub_clean_quant.csv", help="Output cleaned CSV path")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    build_bank(Path(args.input), Path(args.out_json), Path(args.out_csv))
