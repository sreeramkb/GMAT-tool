"""
launcher.py — Terminal menu for the GMAT Focus Edition Simulator (architecture §18).

Offers: start any section (choosing the engine mode), open the master score
report CSV, generate & open the analytics dashboard, or exit. Section runners
are invoked as subprocesses so a crash in one section never takes down the
launcher itself.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent

SECTION_RUNNERS = {
    "Quant": ROOT_DIR / "Quant" / "run_quant_section_v3.py",
    "Verbal": ROOT_DIR / "Verbal" / "run_verbal_section.py",
    "DI": ROOT_DIR / "DI_Adaptive" / "run_di_section.py",
}

MASTER_CSV_PATH = ROOT_DIR / "score_report_master_v2.csv"
ANALYTICS_SCRIPT_PATH = ROOT_DIR / "analytics_dashboard.py"

ENGINE_MODES = [
    ("1", "IRT v1 (pure adaptive)", "v1"),
    ("2", "Banded v2 (percentile-only)", "v2"),
    ("3", "Banded + IRT Hybrid v3 (recommended)", "v3"),
]


def prompt_engine_mode() -> str | None:
    print("\nChoose engine mode:")
    for key, label, _ in ENGINE_MODES:
        print(f"  [{key}] {label}")
    choice = input("Engine mode: ").strip()
    for key, _, mode in ENGINE_MODES:
        if choice == key:
            return mode
    print("Invalid choice.")
    return None


def start_section(section_name: str) -> None:
    runner_path = SECTION_RUNNERS[section_name]
    if not runner_path.exists():
        print(f"\n{section_name} runner not found at {runner_path}. It hasn't been built yet.")
        return

    engine_mode = prompt_engine_mode()
    if engine_mode is None:
        return

    print(f"\nLaunching {section_name} section (engine mode: {engine_mode})...\n")
    subprocess.run(
        [sys.executable, str(runner_path), "--engine-mode", engine_mode],
        cwd=str(runner_path.parent),
    )


def open_master_csv() -> None:
    if not MASTER_CSV_PATH.exists():
        print(f"\nNo master score report found yet at {MASTER_CSV_PATH}.")
        return
    if sys.platform.startswith("win"):
        os.startfile(MASTER_CSV_PATH)  # type: ignore[attr-defined]
    else:
        subprocess.run(["xdg-open" if sys.platform.startswith("linux") else "open", str(MASTER_CSV_PATH)])


def generate_and_open_dashboard() -> None:
    if not ANALYTICS_SCRIPT_PATH.exists():
        print(f"\nanalytics_dashboard.py not found at {ANALYTICS_SCRIPT_PATH}.")
        return
    print("\nGenerating analytics dashboard...\n")
    subprocess.run([sys.executable, str(ANALYTICS_SCRIPT_PATH)], cwd=str(ROOT_DIR))


def print_menu() -> None:
    print("\n=== GMAT Focus Edition Simulator ===")
    print("  [1] Start Quant Section")
    print("  [2] Start Verbal Section")
    print("  [3] Start Data Insights Section")
    print("  [4] Open Master Score Report CSV")
    print("  [5] Generate & Open Analytics Dashboard")
    print("  [6] Exit")


def main() -> None:
    actions = {
        "1": lambda: start_section("Quant"),
        "2": lambda: start_section("Verbal"),
        "3": lambda: start_section("DI"),
        "4": open_master_csv,
        "5": generate_and_open_dashboard,
    }

    while True:
        print_menu()
        choice = input("Select an option: ").strip()

        if choice == "6":
            print("Goodbye.")
            break

        action = actions.get(choice)
        if action is None:
            print("Invalid choice, please try again.")
            continue

        action()


if __name__ == "__main__":
    main()
