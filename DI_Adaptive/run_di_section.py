"""
DI_Adaptive/run_di_section.py — Main test runner for the GMAT Focus Data
Insights section.

Mirrors Quant/run_quant_section_v3.py's browser automation (same
undetected_chromedriver setup, same fixed dom_scraper click/correctness
detection), with DI-specific differences per architecture §3 and §11:

  - Blueprint is randomized per session: Standard (75%) or Extended MSR (25%).
  - The "Graphs and Tables" category has Graphics Interpretation / Table
    Analysis sub-quotas tracked independently of the engine's category state.
  - MSRs are a single GMAT Club page containing multiple (default 3)
    sub-question answer areas; each sub-answer is collected in sequence and
    scored via BandedScorerV3.record_msr_response().

NOTE: the MSR sub-question flow is a first attempt built without a live GMAT
Club MSR page to verify against (per the Quant lesson: selectors that look
reasonable often don't match the real site). Run with GMAT_SIM_DUMP_DOM=1 set
and correct the DOM assumptions the same way §13.3 was fixed for Quant.
"""

from __future__ import annotations

import json
import os
import random
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from selenium.common.exceptions import NoSuchWindowException

try:
    import certifi
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.banded_scorer_v3 import (  # noqa: E402
    BandedScorerV3,
    BAND_TABLE,
    SectionState,
    new_section_state,
)
from engine.dom_scraper import (  # noqa: E402
    dump_answer_area_diagnostics,
    inject_feedback_mask,
    install_submission_interceptor,
    read_dom_timer_seconds,
    read_submission_result,
    scrape_question_stats,
    set_question_context,
)
from engine.hud_overlay import inject_hud, read_review_list  # noqa: E402
from engine.exclusion import (  # noqa: E402
    UsedQuestionLedger,
    build_exclusion_set,
    filter_available_questions,
)
from engine.score_report import append_score_report  # noqa: E402

try:
    import undetected_chromedriver as uc
    _HAVE_UC = True
except ImportError:  # pragma: no cover - allows the module to be imported without the browser stack
    _HAVE_UC = False


# ===========================================================================
# Constants (architecture §3 — DI blueprint, §14 — timer budget)
# ===========================================================================

SECTION_NAME = "DI"
TOTAL_QUESTIONS = 20
TIME_BUDGET_SECONDS = 2700  # 45 minutes of thinking time

GRAPHS_AND_TABLES_CATEGORY = "Graphs and Tables"
MSR_CATEGORY = "MSRs"

# architecture §3 — Standard Blueprint (75% chance)
STANDARD_CATEGORY_QUOTAS = {
    "Data Sufficiency": 7,
    GRAPHS_AND_TABLES_CATEGORY: 6,
    MSR_CATEGORY: 3,
    "Two-Part Analysis": 4,
}
STANDARD_SUBTOPIC_QUOTAS = {"Graphics Interpretation": 3, "Table Analysis": 3}

# architecture §3 — Extended MSR Blueprint (25% chance)
EXTENDED_CATEGORY_QUOTAS = {
    "Data Sufficiency": 6,
    GRAPHS_AND_TABLES_CATEGORY: 5,
    MSR_CATEGORY: 6,
    "Two-Part Analysis": 3,
}
EXTENDED_SUBTOPIC_QUOTAS = {"Graphics Interpretation": 3, "Table Analysis": 2}

STANDARD_BLUEPRINT_WEIGHT = 0.75

BASE_DIR = Path(__file__).resolve().parent
ROOT_DIR = BASE_DIR.parent

QUESTION_BANK_PATH = BASE_DIR / "questions_di_v1.json"
USED_QUESTIONS_LEDGER_PATH = BASE_DIR / "used_questions.json"
ACTIVE_SESSION_PATH = BASE_DIR / "active_session.json"
REPORTS_DIR = BASE_DIR / "di_reports"
MASTER_CSV_PATH = ROOT_DIR / "score_report_master_v2.csv"
CHROME_PROFILE_DIR = ROOT_DIR / ".gmatclub_uc_profile"

ANSWER_WAIT_POLL_SECONDS = 1.0
ANSWER_WAIT_TIMEOUT_SECONDS = 600  # generous per-question ceiling to avoid a hung loop
RESULT_SETTLE_GRACE_SECONDS = 8.0  # let GMAT Club reveal correct/wrong classes before trusting the verdict

MAX_REVIEW_EDITS = 3


# ===========================================================================
# Browser setup (identical approach to the Quant runner)
# ===========================================================================

def launch_driver():
    """Launch undetected_chromedriver with a persistent profile (architecture §13.1)."""
    if not _HAVE_UC:
        raise RuntimeError(
            "undetected_chromedriver is not installed. Run: pip install undetected-chromedriver selenium"
        )
    CHROME_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    chrome_app = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    options = uc.ChromeOptions()
    options.add_argument(f"--user-data-dir={CHROME_PROFILE_DIR}")
    if Path(chrome_app).exists():
        options.binary_location = chrome_app
    # Pin the chromedriver build to the locally installed Chrome's major version.
    # Without this, undetected_chromedriver can grab a driver for a newer Chrome
    # release than what's actually installed and fail with SessionNotCreatedException.
    return uc.Chrome(options=options, version_main=detect_chrome_major_version())


def detect_chrome_major_version() -> Optional[int]:
    """Best-effort detection of the installed Chrome's major version number.

    Reads version metadata instead of running "chrome --version" - launching
    the executable when Chrome is already running can just forward the args
    to the existing instance (e.g. opening a tab) instead of printing a
    version, as observed on Windows.
    """
    try:
        executable = uc.find_chrome_executable()
        if not executable:
            return None
        if sys.platform == "win32":
            output = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command",
                 f"(Get-Item -LiteralPath '{executable}').VersionInfo.FileVersion"],
                text=True,
            )
        elif sys.platform == "darwin":
            # executable is .../Google Chrome.app/Contents/MacOS/Google Chrome;
            # read the bundle's Info.plist instead of launching the binary.
            import plistlib
            app_dir = next(p for p in Path(executable).parents if p.suffix == ".app")
            with (app_dir / "Contents" / "Info.plist").open("rb") as f:
                output = str(plistlib.load(f).get("CFBundleShortVersionString", ""))
        else:
            output = subprocess.check_output([executable, "--version"], text=True)
        match = re.search(r"(\d+)\.", output)
        if match:
            return int(match.group(1))
    except Exception:
        pass
    return None


def is_gmatclub_error_page(driver) -> bool:
    """Return True when GMAT Club is currently returning an outage/HTTP 500 page."""
    try:
        return bool(driver.execute_script("""
            const text = (document.body && document.body.innerText) || '';
            return /(This page isn't working|HTTP ERROR 500|currently unable to handle this request|Please try again later)/i.test(text);
        """))
    except Exception:
        return False


def ensure_logged_in(driver) -> bool:
    """Navigate to GMAT Club and prompt the user to log in if no session cookie is detected."""
    driver.get("https://gmatclub.com/forum/")
    if is_gmatclub_error_page(driver):
        print("GMAT Club is currently unavailable (HTTP 500 / outage page). Please retry later.")
        return False

    is_logged_in = driver.execute_script(
        "return !!document.querySelector('.username, #username_logged_in, a[href*=\"logout\"]');"
    )
    if not is_logged_in:
        print("You don't appear to be logged into GMAT Club.")
        input("Please log in in the opened browser window, then press Enter to continue...")
        if is_gmatclub_error_page(driver):
            print("GMAT Club is still unavailable after login attempt. Stopping cleanly.")
            return False
    return True


# ===========================================================================
# Session persistence
# ===========================================================================

def load_active_session() -> Optional[dict]:
    if ACTIVE_SESSION_PATH.exists():
        try:
            return json.loads(ACTIVE_SESSION_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
    return None


def save_active_session(state: dict) -> None:
    ACTIVE_SESSION_PATH.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")


def clear_active_session() -> None:
    if ACTIVE_SESSION_PATH.exists():
        ACTIVE_SESSION_PATH.unlink()


def section_state_to_dict(section: SectionState) -> dict:
    return {
        "section_name": section.section_name,
        "category_quotas": section.category_quotas,
        "total_questions": section.total_questions,
        "overall_percentile": section.overall_percentile,
        "question_index": section.question_index,
        "answered_by_category": section.answered_by_category,
        "exhausted_categories": list(section.exhausted_categories),
        "early_anchor_percentile": section.early_anchor_percentile,
        "early_anchor_locked": section.early_anchor_locked,
        "global_correct_streak": section.global_correct_streak,
        "category_correct_streak": section.category_correct_streak,
        "early_easy_medium_miss": section.early_easy_medium_miss,
        "early_easy_wrong_count": section.early_easy_wrong_count,
        "section_percentile_for_scale": section.section_percentile_for_scale,
        "scaled_score": section.scaled_score,
        "seed_overall_percentile": section.seed_overall_percentile,
        "categories": {
            name: {
                "attempts": cat.attempts,
                "correct": cat.correct,
                "percentile": cat.percentile,
                "band_index": cat.band_index,
                "streak": cat.streak,
                "last_seen_at": cat.last_seen_at,
                "confidence": cat.confidence,
                "max_band_index_seen": cat.max_band_index_seen,
                "theta": cat.theta,
                "response_history": cat.response_history,
                "recent_bands": cat.recent_bands,
            }
            for name, cat in section.categories.items()
        },
    }


def section_state_from_dict(data: dict) -> SectionState:
    section = new_section_state(
        section_name=data["section_name"],
        category_quotas=data["category_quotas"],
        total_questions=data["total_questions"],
        seed_overall_percentile=data.get("seed_overall_percentile", 60.0),
    )
    section.overall_percentile = data["overall_percentile"]
    section.question_index = data["question_index"]
    section.answered_by_category = data["answered_by_category"]
    section.exhausted_categories = set(data["exhausted_categories"])
    section.early_anchor_percentile = data["early_anchor_percentile"]
    section.early_anchor_locked = data["early_anchor_locked"]
    section.global_correct_streak = data["global_correct_streak"]
    section.category_correct_streak = data["category_correct_streak"]
    section.early_easy_medium_miss = data["early_easy_medium_miss"]
    section.early_easy_wrong_count = data["early_easy_wrong_count"]
    section.section_percentile_for_scale = data["section_percentile_for_scale"]
    section.scaled_score = data["scaled_score"]

    for name, cat_data in data["categories"].items():
        cat = section.categories[name]
        cat.attempts = cat_data["attempts"]
        cat.correct = cat_data["correct"]
        cat.percentile = cat_data["percentile"]
        cat.band_index = cat_data["band_index"]
        cat.streak = cat_data["streak"]
        cat.last_seen_at = cat_data["last_seen_at"]
        cat.confidence = cat_data["confidence"]
        cat.max_band_index_seen = cat_data["max_band_index_seen"]
        cat.theta = cat_data["theta"]
        cat.response_history = [tuple(r) for r in cat_data["response_history"]]
        cat.recent_bands = [tuple(r) for r in cat_data["recent_bands"]]

    return section


# ===========================================================================
# Prompts
# ===========================================================================

def prompt_resume() -> bool:
    answer = input("An in-progress session was found. Resume it? [Y/n]: ").strip().lower()
    return answer in ("", "y", "yes")


def prompt_start_params() -> float:
    percentile_raw = input("Starting percentile (default 60): ").strip()
    return float(percentile_raw) if percentile_raw else 60.0


def choose_blueprint() -> Tuple[Dict[str, int], Dict[str, int]]:
    """Randomize the DI blueprint per architecture §3: Standard (75%) or Extended MSR (25%)."""
    if random.random() < STANDARD_BLUEPRINT_WEIGHT:
        print("Blueprint: Standard (DS 7 / Graphs & Tables 6 / MSRs 3 / Two-Part 4)")
        return dict(STANDARD_CATEGORY_QUOTAS), dict(STANDARD_SUBTOPIC_QUOTAS)
    print("Blueprint: Extended MSR (DS 6 / Graphs & Tables 5 / MSRs 6 / Two-Part 3)")
    return dict(EXTENDED_CATEGORY_QUOTAS), dict(EXTENDED_SUBTOPIC_QUOTAS)


# ===========================================================================
# Main question loop
# ===========================================================================

def load_question_bank() -> List[dict]:
    if not QUESTION_BANK_PATH.exists():
        raise FileNotFoundError(
            f"Question bank not found: {QUESTION_BANK_PATH}\n"
            f"Run build_di_bank.py against a GMAT Club Data Insights CSV export first."
        )
    return json.loads(QUESTION_BANK_PATH.read_text(encoding="utf-8"))


def filter_by_subtopic_quota(
    bank: List[dict], subtopic_answered: Dict[str, int], subtopic_quotas: Dict[str, int]
) -> List[dict]:
    """Exclude Graphs and Tables candidates whose Graphics/Table sub-quota is already met."""
    result = []
    for q in bank:
        if q.get("category") == GRAPHS_AND_TABLES_CATEGORY:
            subtopic = q.get("subtopic")
            if subtopic in subtopic_quotas and subtopic_answered.get(subtopic, 0) >= subtopic_quotas[subtopic]:
                continue
        result.append(q)
    return result


def wait_for_answer(driver, dump_tag: str = "") -> Optional[dict]:
    """Use a tool-managed per-question timer and capture the page selection without clicking the site's timer."""
    deadline = time.monotonic() + ANSWER_WAIT_TIMEOUT_SECONDS
    print(f"Question timer started: {ANSWER_WAIT_TIMEOUT_SECONDS}s")
    elapsed = 0.0
    pending_result: Optional[dict] = None
    pending_since: Optional[float] = None
    announced_selection = False

    while time.monotonic() < deadline:
        result = read_submission_result(driver)

        if result is not None and result.get("was_correct") is not None:
            return result

        if result is not None:
            # A click was captured but the page hasn't revealed correctness yet
            # (e.g. GMAT Club's AJAX response / class update hasn't landed). Keep
            # polling for a short settle window instead of trusting this
            # placeholder immediately.
            if not announced_selection:
                selected_choice = result.get("selected_choice")
                if selected_choice:
                    print(f"Detected selected option: {selected_choice} (waiting for verdict...)")
                announced_selection = True
            if pending_since is None:
                pending_since = time.monotonic()
            pending_result = result
            if time.monotonic() - pending_since < RESULT_SETTLE_GRACE_SECONDS:
                time.sleep(ANSWER_WAIT_POLL_SECONDS)
                elapsed += ANSWER_WAIT_POLL_SECONDS
                continue
            if os.environ.get("GMAT_SIM_DUMP_DOM") == "1":
                dump_path = BASE_DIR / f"dom_debug_after_click{dump_tag}.json"
                dump_path.write_text(
                    json.dumps(dump_answer_area_diagnostics(driver), indent=2),
                    encoding="utf-8",
                )
                print(f"Dumped post-click answer-area diagnostics to {dump_path}")
            return pending_result

        if elapsed % 10 < ANSWER_WAIT_POLL_SECONDS:
            remaining = max(0, int(deadline - time.monotonic()))
            print(f"Waiting for answer... {remaining}s remaining")

        time.sleep(ANSWER_WAIT_POLL_SECONDS)
        elapsed += ANSWER_WAIT_POLL_SECONDS

    print("Question timer expired before the answer was captured.")
    return pending_result or {"was_correct": None, "selected_choice": None, "correct_choice": None}


def prompt_manual_correctness() -> bool:
    """Fallback if automatic DOM detection fails to capture a result in time."""
    answer = input("Could not auto-detect the result. Was your answer correct? [y/n]: ").strip().lower()
    return answer.startswith("y")


def resolve_single_answer(driver, dump_tag: str = "") -> bool:
    """Wait for one answer submission and return whether it was correct."""
    result = wait_for_answer(driver, dump_tag=dump_tag)
    stats = None
    if result is not None:
        selected = result.get("selected_choice")
        correct = result.get("correct_choice")
        if selected:
            print(f"Captured selected answer: {selected}")
        if correct:
            print(f"Forum correct answer for this question: {correct}")
        if result.get("was_correct") is not None:
            was_correct = bool(result.get("was_correct"))
            print(f"Answer verdict: {'CORRECT' if was_correct else 'WRONG'}")
            if selected and correct:
                print(f"Selected {selected} vs correct {correct} -> {'MATCH' if selected == correct else 'MISMATCH'}")
        else:
            was_correct = prompt_manual_correctness()
            print(f"Answer verdict: {'CORRECT' if was_correct else 'WRONG'}")
    else:
        was_correct = prompt_manual_correctness()
        print(f"Answer verdict: {'CORRECT' if was_correct else 'WRONG'}")
    return was_correct


def run_msr_question(driver, question: dict) -> List[Tuple[int, bool]]:
    """Collect answers for each MSR sub-question on the same page (architecture §11).

    GMAT Club presents all MSR sub-questions as separate answer areas on one
    page. Between sub-questions we reset the tool's tracked answer state so
    each one is captured independently; the sub-question's own DOM update
    (e.g. a tab switch) re-triggers install_submission_interceptor's
    MutationObserver without a full page reload.
    """
    child_count = question.get("msr_child_count", 3)
    child_difficulties = question.get("msr_child_difficulty_indices", [question["difficulty_index"]] * child_count)
    children: List[Tuple[int, bool]] = []

    for i in range(child_count):
        print(f"\n--- MSR sub-question {i + 1}/{child_count} ---")
        # Re-use the real page URL (not a synthetic per-child key) - the
        # interceptor's guard compares against the live window.location.href,
        # which doesn't change between MSR sub-question tabs on the same page.
        set_question_context(driver, question["url"])
        install_submission_interceptor(driver)
        was_correct = resolve_single_answer(driver, dump_tag=f"_msr{i}")
        children.append((child_difficulties[i], was_correct))
        if i < child_count - 1:
            input("Press Enter once the next MSR sub-question is visible...")

    return children


def run_question_loop(
    driver,
    scorer: BandedScorerV3,
    bank: List[dict],
    ledger: UsedQuestionLedger,
    session_id: str,
    served_ids: set,
    elapsed_thinking_seconds: float,
    diagnostics_log: List[dict],
    subtopic_quotas: Dict[str, int],
    subtopic_answered: Dict[str, int],
) -> float:
    section = scorer.section

    while section.question_index < section.total_questions:
        remaining_seconds = TIME_BUDGET_SECONDS - elapsed_thinking_seconds
        if remaining_seconds <= 0:
            print("Time expired.")
            break

        excluded_ids = build_exclusion_set(ledger, served_ids)
        available = filter_available_questions(bank, excluded_ids)
        available = filter_by_subtopic_quota(available, subtopic_answered, subtopic_quotas)

        question = scorer.select_question(available)
        if question is None:
            print("No more available questions matching quotas/exclusions. Ending section early.")
            break

        driver.get(question["url"])
        set_question_context(driver, question["url"])
        question_started_at = time.monotonic()
        inject_feedback_mask(driver)
        inject_hud(
            driver,
            question_index=section.question_index + 1,
            total_questions=section.total_questions,
            remaining_seconds=int(remaining_seconds),
            question_id=question["id"],
        )
        install_submission_interceptor(driver)
        print("Using the tool timer; GMAT Club timer start is intentionally skipped to keep the current question open.")

        if os.environ.get("GMAT_SIM_DUMP_DOM") == "1":
            dump_path = BASE_DIR / "dom_debug.json"
            dump_path.write_text(
                json.dumps(dump_answer_area_diagnostics(driver), indent=2),
                encoding="utf-8",
            )
            print(f"Dumped answer-area diagnostics to {dump_path}")

        if question["delivery_type"] == "MSR":
            children = run_msr_question(driver, question)
            was_correct = all(correct for _, correct in children)
            scorer.record_msr_response(question["category"], children)
        else:
            was_correct = resolve_single_answer(driver)
            scorer.record_response(question, was_correct)

        stats = scrape_question_stats(driver)
        dom_elapsed = read_dom_timer_seconds(driver)

        wall_elapsed = time.monotonic() - question_started_at
        question_elapsed = dom_elapsed if dom_elapsed and dom_elapsed > 0 else max(wall_elapsed, ANSWER_WAIT_POLL_SECONDS)
        elapsed_thinking_seconds += question_elapsed

        if question["category"] == GRAPHS_AND_TABLES_CATEGORY:
            subtopic_answered[question["subtopic"]] = subtopic_answered.get(question["subtopic"], 0) + 1

        served_ids.add(question["id"])
        ledger.record(question["id"], question["url"], session_id)

        diagnostics_log.append({
            "question_id": question["id"],
            "url": question["url"],
            "category": question["category"],
            "subtopic": question.get("subtopic"),
            "delivery_type": question["delivery_type"],
            "difficulty_index": question["difficulty_index"],
            "was_correct": was_correct,
            "elapsed_seconds": question_elapsed,
            "dom_stats": stats,
            "theta_after": section.categories[question["category"]].theta,
            "overall_percentile_after": section.overall_percentile,
        })

        save_active_session({
            "session_id": session_id,
            "served_ids": list(served_ids),
            "elapsed_thinking_seconds": elapsed_thinking_seconds,
            "diagnostics_log": diagnostics_log,
            "section_state": section_state_to_dict(section),
            "subtopic_quotas": subtopic_quotas,
            "subtopic_answered": subtopic_answered,
        })

        print(
            f"Q{section.question_index}/{section.total_questions} "
            f"[{question['category']}] Band {question['difficulty_index']}: "
            f"{'CORRECT' if was_correct else 'WRONG'} "
            f"(overall percentile: {section.overall_percentile:.1f})"
        )

    return elapsed_thinking_seconds


# ===========================================================================
# Review phase (architecture §15)
# ===========================================================================

def run_review_phase(driver, diagnostics_log: List[dict]) -> None:
    if not diagnostics_log:
        return

    bookmarked = set(read_review_list(driver) or [])

    print("\n--- Review Phase ---")
    print(f"{'#':<4}{'Category':<25}{'Subtopic':<25}{'Band':<6}{'Bookmarked':<12}{'URL'}")
    for i, entry in enumerate(diagnostics_log, start=1):
        flag = "*" if entry["question_id"] in bookmarked else ""
        print(f"{i:<4}{entry['category']:<25}{(entry.get('subtopic') or ''):<25}{entry['difficulty_index']:<6}{flag:<12}{entry['url']}")

    edits_used = 0
    while edits_used < MAX_REVIEW_EDITS:
        choice = input(
            f"\nEnter a question number to revisit ({MAX_REVIEW_EDITS - edits_used} edits left), or 'done': "
        ).strip().lower()
        if choice == "done":
            break
        if not choice.isdigit() or not (1 <= int(choice) <= len(diagnostics_log)):
            print("Invalid entry.")
            continue

        entry = diagnostics_log[int(choice) - 1]
        driver.get(entry["url"])
        inject_feedback_mask(driver)
        new_answer = input("Submit a different answer? Was it correct this time? [y/n/skip]: ").strip().lower()
        if new_answer == "skip":
            continue
        was_correct = new_answer.startswith("y")
        if was_correct != entry["was_correct"]:
            entry["was_correct"] = was_correct
            edits_used += 1
            print(f"Answer updated. {MAX_REVIEW_EDITS - edits_used} edits remaining.")


# ===========================================================================
# Finalization
# ===========================================================================

def compute_accuracy_by_band(diagnostics_log: List[dict]) -> Dict[str, float]:
    totals: Dict[int, int] = {}
    corrects: Dict[int, int] = {}
    for entry in diagnostics_log:
        band = entry["difficulty_index"]
        totals[band] = totals.get(band, 0) + 1
        if entry["was_correct"]:
            corrects[band] = corrects.get(band, 0) + 1
    return {
        BAND_TABLE[band]["label"]: corrects.get(band, 0) / total
        for band, total in totals.items()
    }


def compute_accuracy_by_category(diagnostics_log: List[dict]) -> Dict[str, float]:
    totals: Dict[str, int] = {}
    corrects: Dict[str, int] = {}
    for entry in diagnostics_log:
        cat = entry["category"]
        totals[cat] = totals.get(cat, 0) + 1
        if entry["was_correct"]:
            corrects[cat] = corrects.get(cat, 0) + 1
    return {cat: corrects.get(cat, 0) / total for cat, total in totals.items()}


def finalize_and_report(
    scorer: BandedScorerV3,
    session_id: str,
    diagnostics_log: List[dict],
    elapsed_thinking_seconds: float,
) -> dict:
    section = scorer.section
    n_unanswered = max(0, section.total_questions - section.question_index)

    final_result = scorer.finalize_section(n_unanswered=n_unanswered)

    accuracy = (
        sum(1 for e in diagnostics_log if e["was_correct"]) / len(diagnostics_log)
        if diagnostics_log else 0.0
    )
    theta_trajectory = [e["theta_after"] for e in diagnostics_log]

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORTS_DIR / f"{session_id}.json"
    report_path.write_text(json.dumps({
        "session_id": session_id,
        "section": SECTION_NAME,
        "final_result": final_result,
        "diagnostics_log": diagnostics_log,
        "elapsed_thinking_seconds": elapsed_thinking_seconds,
    }, indent=2, default=str), encoding="utf-8")

    append_score_report(
        csv_path=MASTER_CSV_PATH,
        session_id=session_id,
        section=SECTION_NAME,
        engine_mode="v3_hybrid",
        scaled_score=final_result["scaled_score"],
        accuracy=accuracy,
        time_used_seconds=elapsed_thinking_seconds,
        theta_trajectory=theta_trajectory,
        accuracy_by_band=compute_accuracy_by_band(diagnostics_log),
        accuracy_by_category=compute_accuracy_by_category(diagnostics_log),
        diagnostics=final_result,
    )

    return final_result


# ===========================================================================
# Entry point
# ===========================================================================

def main() -> None:
    print("=== GMAT Focus Data Insights Section (V3 Hybrid Engine) ===")

    resumed_state = load_active_session()
    session_id: str
    served_ids: set
    diagnostics_log: List[dict]
    elapsed_thinking_seconds: float
    subtopic_quotas: Dict[str, int]
    subtopic_answered: Dict[str, int]

    if resumed_state and prompt_resume():
        session_id = resumed_state["session_id"]
        served_ids = set(resumed_state["served_ids"])
        elapsed_thinking_seconds = resumed_state["elapsed_thinking_seconds"]
        diagnostics_log = resumed_state["diagnostics_log"]
        section = section_state_from_dict(resumed_state["section_state"])
        subtopic_quotas = resumed_state.get("subtopic_quotas", dict(STANDARD_SUBTOPIC_QUOTAS))
        subtopic_answered = resumed_state.get("subtopic_answered", {})
    else:
        clear_active_session()
        session_id = str(uuid.uuid4())
        served_ids = set()
        elapsed_thinking_seconds = 0.0
        diagnostics_log = []
        subtopic_answered = {}
        starting_percentile = prompt_start_params()
        category_quotas, subtopic_quotas = choose_blueprint()
        section = new_section_state(
            section_name=SECTION_NAME,
            category_quotas=category_quotas,
            total_questions=TOTAL_QUESTIONS,
            seed_overall_percentile=starting_percentile,
        )

    scorer = BandedScorerV3(section)
    bank = load_question_bank()
    ledger = UsedQuestionLedger(USED_QUESTIONS_LEDGER_PATH)

    driver = launch_driver()
    try:
        try:
            if not ensure_logged_in(driver):
                print("Aborting section because GMAT Club is unavailable.")
                return

            elapsed_thinking_seconds = run_question_loop(
                driver, scorer, bank, ledger, session_id, served_ids,
                elapsed_thinking_seconds, diagnostics_log,
                subtopic_quotas, subtopic_answered,
            )
            run_review_phase(driver, diagnostics_log)
            final_result = finalize_and_report(scorer, session_id, diagnostics_log, elapsed_thinking_seconds)
            print("\n=== Final Score ===")
            print(f"Scaled Score: {final_result['scaled_score']}")
            print(f"Section Percentile: {final_result['section_percentile_for_scale']:.1f}")
            if final_result["early_easy_medium_miss"]:
                print("Note: An early easy/medium miss capped your maximum possible score.")
            clear_active_session()
        except NoSuchWindowException:
            print("Chrome window was closed. Please keep the browser open while the session runs. Exiting cleanly.")
            return
    finally:
        try:
            driver.quit()
        except Exception:
            pass


if __name__ == "__main__":
    main()
