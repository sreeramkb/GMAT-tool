"""
engine/banded_scorer_v3.py — GMAT Focus Edition V3 Hybrid Scoring Engine

Implements the dual-track system described in the architecture document:
  - Track 1 (Percentile System): drives the final scaled score.
  - Track 2 (IRT Theta System): drives question selection via MAP estimation
    and Fisher-Information-based item ranking.

All constants are defined at module level, exactly as specified in the
architecture doc, so they can be tuned without touching engine logic.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

try:
    from scipy.optimize import minimize_scalar
    _HAVE_SCIPY = True
except ImportError:  # pragma: no cover - fallback path
    _HAVE_SCIPY = False


# ===========================================================================
# Section 5 — IRT Parameters & Band Mapping
# ===========================================================================

# band_index -> (label, baseline_pct, b (difficulty), a (discrimination))
BAND_TABLE: Dict[int, Dict[str, float]] = {
    1: {"label": "Very Easy",       "baseline_pct": 20.0, "b": -1.60, "a": 0.6},
    2: {"label": "Easy",            "baseline_pct": 33.0, "b": -0.90, "a": 0.8},
    3: {"label": "Easy-Medium",     "baseline_pct": 50.0, "b": -0.60, "a": 0.8},
    4: {"label": "Medium",          "baseline_pct": 70.0, "b": -0.25, "a": 1.0},
    5: {"label": "Hard",            "baseline_pct": 85.0, "b": 0.15,  "a": 1.1},
    6: {"label": "Very Hard",       "baseline_pct": 95.0, "b": 0.42,  "a": 1.2},
    7: {"label": "Extreme",         "baseline_pct": 99.0, "b": 1.00,  "a": 1.3},
    8: {"label": "Abnormally Hard", "baseline_pct": 99.5, "b": 1.50,  "a": 1.3},
}

PSEUDO_GUESSING_C = 0.20  # universal 3PL c-parameter for 5-option questions

THETA_MIN, THETA_MAX = -3.5, 3.5


# ===========================================================================
# Section 6 — Track 1: Percentile System (Delta Tables)
# ===========================================================================

DELTA_CORRECT = {1: 4, 2: 5, 3: 6, 4: 7, 5: 8, 6: 9, 7: 10, 8: 11}
DELTA_WRONG = {1: -10, 2: -9, 3: -8, 4: -7, 5: -5, 6: -3, 7: -1, 8: -0.5}

CHILD_DELTA_CORRECT = {1: 3, 2: 4, 3: 5, 4: 6, 5: 7, 6: 8, 7: 9, 8: 10}
CHILD_DELTA_WRONG = {1: -10, 2: -9, 3: -8, 4: -7, 5: -5, 6: -3, 7: -1, 8: -0.5}

# §6.3 — overall-percentile correct-answer caps, keyed by band
OVERALL_CORRECT_CAP = {1: 1.5, 2: 1.5, 3: 1.5, 4: 1.5, 5: 2.5, 6: 3.6, 7: 4.0, 8: 4.5}
OVERALL_DELTA_DAMPING = 0.4  # delta_overall = 0.4 * delta_category (base, before caps)


# ===========================================================================
# Section 9 — Early Profile Anchor & Penalty Logic
# ===========================================================================

EARLY_PROFILE_QUESTIONS = 6
EARLY_ANCHOR_BLEND_ANCHOR_WEIGHT = 0.80
EARLY_ANCHOR_BLEND_LIVE_WEIGHT = 0.20

EARLY_WRONG_PENALTY = {1: -20.0, 2: -18.0, 3: -15.0, 4: -12.0, 5: -5.0, 6: -3.0, 7: -1.5, 8: -1.0}
LATER_WRONG_PENALTY = {1: -8.0, 2: -7.0, 3: -6.0, 4: -5.0, 5: -3.5, 6: -2.0, 7: -1.0, 8: -0.5}

EARLY_EASY_MEDIUM_MISS_MAX_BAND = 4  # bands <= this trigger the early-miss flag
EARLY_MISS_MAX_SCALED_SCORE = 84
EARLY_MISS_CEILING_BONUS = 12.0
EARLY_MISS_CEILING_FLOOR = 70.0


# ===========================================================================
# Section 12 — Final Score Computation (piecewise percentile -> scaled score)
# ===========================================================================

QUANT_MAPPING: List[Tuple[float, int]] = [
    (1, 61), (2, 63), (3, 64), (4, 65), (5, 66), (7, 67), (9, 68), (11, 69),
    (14, 70), (17, 71), (20, 72), (23, 73), (28, 74), (33, 75), (38, 76),
    (43, 77), (50, 78), (57, 79), (64, 80), (70, 81), (75, 82), (80, 83),
    (85, 84), (88, 85), (91, 86), (93, 87), (95, 88), (97, 89), (100, 90),
]

DI_MAPPING: List[Tuple[float, int]] = [
    (3, 60), (4, 61), (5, 63), (7, 64), (8, 65), (10, 66), (11, 67), (14, 68),
    (17, 69), (20, 70), (24, 71), (29, 72), (34, 73), (40, 74), (46, 75),
    (52, 76), (61, 77), (69, 78), (76, 79), (83, 80), (88, 81), (93, 82),
    (95, 83), (97, 84), (98, 85), (99, 88), (100, 90),
]

# Verbal reuses the Quant mapping as a starting point (architecture §12.3)
VERBAL_MAPPING: List[Tuple[float, int]] = QUANT_MAPPING

SECTION_MAPPINGS = {
    "Quant": QUANT_MAPPING,
    "DI": DI_MAPPING,
    "Verbal": VERBAL_MAPPING,
}

RAW_SECTION_PERCENTILE_CATEGORY_WEIGHT = 0.75
RAW_SECTION_PERCENTILE_OVERALL_WEIGHT = 0.25


# ===========================================================================
# Section 4 — State Model
# ===========================================================================

@dataclass
class CategoryState:
    name: str
    attempts: int = 0
    correct: int = 0
    percentile: float = 60.0
    band_index: float = 4.0
    streak: int = 0
    last_seen_at: int = -1
    confidence: float = 0.0
    max_band_index_seen: int = 0
    theta: float = 0.0
    # (b, a, c, was_correct) per observation, MSR children included individually
    response_history: List[Tuple[float, float, float, bool]] = field(default_factory=list)
    # last 3 (band_index, was_correct) used for ceiling-probe detection
    recent_bands: List[Tuple[int, bool]] = field(default_factory=list)


@dataclass
class SectionState:
    section_name: str
    categories: Dict[str, CategoryState]
    category_quotas: Dict[str, int]
    total_questions: int = 21
    overall_percentile: float = 60.0
    question_index: int = 0
    answered_by_category: Dict[str, int] = field(default_factory=dict)
    exhausted_categories: set = field(default_factory=set)

    # Early anchor
    early_anchor_percentile: float = 60.0
    early_anchor_locked: bool = False

    # Streaks
    global_correct_streak: int = 0
    category_correct_streak: Dict[str, int] = field(default_factory=dict)
    early_jump_level: int = 0

    # Early miss tracking
    early_easy_medium_miss: bool = False
    early_easy_wrong_count: int = 0
    section_percentile_for_scale: float = 60.0
    scaled_score: int = 60

    # Seed
    seed_overall_percentile: float = 60.0

    def __post_init__(self) -> None:
        for name in self.category_quotas:
            self.answered_by_category.setdefault(name, 0)
            self.category_correct_streak.setdefault(name, 0)


# ===========================================================================
# Section 7 — Track 2: IRT Theta System
# ===========================================================================

def p_correct(theta: float, b: float, a: float, c: float = PSEUDO_GUESSING_C) -> float:
    """3PL probability of a correct response (no 1.7 scaling constant per §7.3 note)."""
    exponent = -1.7 * a * (theta - b)
    return c + (1.0 - c) / (1.0 + math.exp(exponent))


def _log_posterior(theta: float, history: Sequence[Tuple[float, float, float, bool]], sigma: float) -> float:
    log_likelihood = 0.0
    for b, a, c, was_correct in history:
        p = p_correct(theta, b, a, c)
        p = min(max(p, 1e-9), 1 - 1e-9)
        log_likelihood += math.log(p) if was_correct else math.log(1 - p)
    log_prior = -(theta ** 2) / (2 * sigma ** 2)
    return log_likelihood + log_prior


def estimate_theta_map(history: Sequence[Tuple[float, float, float, bool]]) -> float:
    """MAP estimation of theta from the (b, a, c, correct) response history."""
    if not history:
        return 0.0

    sigma = 1.5 if len(history) <= 10 else 2.0

    if _HAVE_SCIPY:
        result = minimize_scalar(
            lambda theta: -_log_posterior(theta, history, sigma),
            bounds=(THETA_MIN, THETA_MAX),
            method="bounded",
        )
        return float(min(max(result.x, THETA_MIN), THETA_MAX))

    # Grid-search fallback (step 0.1) for environments without scipy
    best_theta, best_log_posterior = 0.0, -math.inf
    theta = THETA_MIN
    while theta <= THETA_MAX + 1e-9:
        lp = _log_posterior(theta, history, sigma)
        if lp > best_log_posterior:
            best_log_posterior, best_theta = lp, theta
        theta += 0.1
    return best_theta


def fisher_information(theta: float, b: float, a: float, c: float = PSEUDO_GUESSING_C) -> float:
    """3PL Fisher information, deliberately omitting the 1.7^2 constant (§7.3)."""
    p = p_correct(theta, b, a, c)
    p = min(max(p, 1e-9), 1 - 1e-9)
    return (a ** 2) * ((p - c) ** 2 / (1 - c) ** 2) * ((1 - p) / p)


def composite_score(theta: float, b: float, a: float, c: float = PSEUDO_GUESSING_C) -> float:
    info = fisher_information(theta, b, a, c)
    return info + 0.05 * max(0.0, b - theta)


# ===========================================================================
# Section 6.5 — Percentile <-> Band conversion
# ===========================================================================

def percentile_to_band_index(p: float) -> int:
    if p < 25:
        return 1
    if p < 40:
        return 2
    if p < 60:
        return 3
    if p < 80:
        return 4
    if p < 93:
        return 5
    if p < 98:
        return 6
    if p < 99.5:
        return 7
    return 8


# ===========================================================================
# Section 12.1 — Normal CDF (for final category percentile from theta)
# ===========================================================================

def norm_cdf(x: float) -> float:
    return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0


# ===========================================================================
# Section 12.3 — Piecewise percentile -> scaled score interpolation
# ===========================================================================

def percentile_to_scaled_score(percentile: float, mapping: List[Tuple[float, int]]) -> int:
    percentile = min(max(percentile, mapping[0][0]), mapping[-1][0])

    for (p_lo, s_lo), (p_hi, s_hi) in zip(mapping, mapping[1:]):
        if p_lo <= percentile <= p_hi:
            if p_hi == p_lo:
                return s_hi
            frac = (percentile - p_lo) / (p_hi - p_lo)
            return round(s_lo + frac * (s_hi - s_lo))

    return mapping[-1][1]


# ===========================================================================
# Section 14.3 — Unanswered penalty
# ===========================================================================

def apply_unanswered_penalty(theta: float, n_unanswered: int) -> float:
    penalized = theta - 0.4 * n_unanswered
    return min(max(penalized, THETA_MIN), THETA_MAX)


# ===========================================================================
# The V3 Hybrid Engine
# ===========================================================================

class BandedScorerV3:
    """Runs the dual-track (percentile + IRT theta) engine for one section."""

    def __init__(self, section: SectionState):
        self.section = section

    # ------------------------------------------------------------------
    # Section 8.1 — Category selection priority formula
    # ------------------------------------------------------------------

    def _quota_gap_norm(self, cat_name: str) -> float:
        quota = self.section.category_quotas[cat_name]
        answered = self.section.answered_by_category[cat_name]
        if quota <= 0:
            return 0.0
        return max(0.0, (quota - answered) / quota)

    def _age_norm(self, cat: CategoryState) -> float:
        if cat.last_seen_at < 0:
            return 1.0
        age = self.section.question_index - cat.last_seen_at
        return min(1.0, age / max(1, self.section.total_questions))

    def category_priority(self, cat_name: str) -> float:
        cat = self.section.categories[cat_name]
        quota_gap_norm = self._quota_gap_norm(cat_name)
        low_confidence = 1.0 - cat.confidence
        age_norm = self._age_norm(cat)
        return 0.5 * quota_gap_norm + 0.3 * low_confidence + 0.2 * age_norm

    def select_category(self) -> Optional[str]:
        available = [
            name for name in self.section.category_quotas
            if name not in self.section.exhausted_categories
            and self.section.answered_by_category[name] < self.section.category_quotas[name]
        ]
        if not available:
            return None

        scored = [(self.category_priority(name), name) for name in available]
        best_score = max(score for score, _ in scored)
        # ties (within a small epsilon) broken randomly
        top = [name for score, name in scored if abs(score - best_score) < 1e-9]
        return random.choice(top)

    # ------------------------------------------------------------------
    # Sections 8.2 - 8.4 — IRT windowing, ceiling probe, final selection
    # ------------------------------------------------------------------

    def _is_ceiling_probe_active(self, cat: CategoryState) -> bool:
        if len(cat.recent_bands) < 3:
            return False
        last_three = cat.recent_bands[-3:]
        all_correct = all(correct for _, correct in last_three)
        all_hard_enough = all(band >= cat.theta - 0.2 for band, correct in last_three)
        return all_correct and all_hard_enough

    def _candidates_in_range(self, bank: List[dict], category: str, lo: float, hi: float) -> List[dict]:
        return [
            q for q in bank
            if q.get("category") == category
            and q.get("active", True)
            and lo <= q["difficulty_anchor"] <= hi
        ]

    def get_candidate_window(self, bank: List[dict], category: str) -> List[dict]:
        cat = self.section.categories[category]
        theta = cat.theta

        if self._is_ceiling_probe_active(cat):
            probe_lo = max(theta + 0.5, 0.42)
            probe_hi = 1.55
            probe_candidates = self._candidates_in_range(bank, category, probe_lo, probe_hi)
            if probe_candidates:
                return probe_candidates
            # fall back to the hardest available items in the category
            all_in_cat = [q for q in bank if q.get("category") == category and q.get("active", True)]
            if all_in_cat:
                hardest = max(q["difficulty_anchor"] for q in all_in_cat)
                return [q for q in all_in_cat if q["difficulty_anchor"] == hardest]

        # Tight window
        tight = self._candidates_in_range(bank, category, theta - 0.5, theta + 0.5)
        if len(tight) >= 3:
            return tight

        # Wider fallback
        wide = self._candidates_in_range(bank, category, theta - 1.0, theta + 1.0)
        if len(wide) >= 3:
            return wide

        # Full fallback: everything in the category
        return [q for q in bank if q.get("category") == category and q.get("active", True)]

    def select_question(self, bank: List[dict]) -> Optional[dict]:
        category = self.select_category()
        if category is None:
            return None

        candidates = self.get_candidate_window(bank, category)
        if not candidates:
            return None

        cat = self.section.categories[category]
        ranked = sorted(
            candidates,
            key=lambda q: composite_score(cat.theta, q["difficulty_anchor"], self._item_a(q), PSEUDO_GUESSING_C),
            reverse=True,
        )
        top_five = ranked[:5]
        return random.choice(top_five)

    @staticmethod
    def _item_a(question: dict) -> float:
        band_index = question["difficulty_index"]
        return BAND_TABLE[band_index]["a"]

    # ------------------------------------------------------------------
    # Section 9 — Early anchor
    # ------------------------------------------------------------------

    def maybe_lock_early_anchor(self) -> None:
        if not self.section.early_anchor_locked and self.section.question_index >= EARLY_PROFILE_QUESTIONS:
            self.section.early_anchor_percentile = self.section.overall_percentile
            self.section.early_anchor_locked = True

    def effective_percentile(self) -> float:
        if not self.section.early_anchor_locked:
            return self.section.overall_percentile
        return (
            EARLY_ANCHOR_BLEND_ANCHOR_WEIGHT * self.section.early_anchor_percentile
            + EARLY_ANCHOR_BLEND_LIVE_WEIGHT * self.section.overall_percentile
        )

    # ------------------------------------------------------------------
    # Section 10 — Streak-based band jump logic
    # ------------------------------------------------------------------

    def _global_jump(self) -> int:
        streak = self.section.global_correct_streak
        if streak <= 1:
            return 0
        if streak <= 3:
            return 1
        return 2

    def _category_limit(self, category: str) -> int:
        return 2 if self.section.category_correct_streak[category] >= 2 else 1

    def effective_jump(self, category: str, in_early_phase: bool) -> int:
        if self.section.early_easy_medium_miss:
            return 0  # jumps blocked entirely after an early easy/medium miss

        jump = min(self._global_jump(), self._category_limit(category))

        if in_early_phase:
            cat = self.section.categories[category]
            # target never exceeds Band 6 during the early phase
            max_allowed_jump = max(0, 6 - int(round(cat.band_index)))
            jump = min(jump, max_allowed_jump)

        return jump

    def can_access_band_8(self, category: str) -> bool:
        if self.section.early_easy_medium_miss:
            return False
        return (
            self.section.global_correct_streak >= 4
            and self.section.category_correct_streak[category] >= 2
            and self.effective_percentile() >= 85
        )

    # ------------------------------------------------------------------
    # Section 6 / 9 — Percentile updates for a single-part question
    # ------------------------------------------------------------------

    def _is_early_phase(self) -> bool:
        return self.section.question_index <= EARLY_PROFILE_QUESTIONS

    def record_response(self, question: dict, was_correct: bool) -> None:
        category = question["category"]
        band_index = question["difficulty_index"]
        cat = self.section.categories[category]
        band_data = BAND_TABLE[band_index]
        b, a, c = band_data["b"], band_data["a"], PSEUDO_GUESSING_C

        self.section.question_index += 1
        cat.attempts += 1
        cat.last_seen_at = self.section.question_index
        self.section.answered_by_category[category] += 1
        if self.section.answered_by_category[category] >= self.section.category_quotas[category]:
            self.section.exhausted_categories.add(category)

        cat.recent_bands.append((band_index, was_correct))
        cat.recent_bands = cat.recent_bands[-3:]

        # ---- Track 1: category percentile delta ----
        delta_category = DELTA_CORRECT[band_index] if was_correct else DELTA_WRONG[band_index]
        cat.percentile = max(0.0, min(100.0, cat.percentile + delta_category))

        # ---- Track 1: overall percentile delta (§6.3) ----
        if was_correct:
            delta_overall = OVERALL_DELTA_DAMPING * delta_category
            delta_overall = min(delta_overall, OVERALL_CORRECT_CAP[band_index])
        else:
            penalty_table = EARLY_WRONG_PENALTY if self._is_early_phase() else LATER_WRONG_PENALTY
            delta_overall = penalty_table[band_index]

        self.section.overall_percentile = max(0.0, min(100.0, self.section.overall_percentile + delta_overall))

        # ---- Band index smoothing (§6.4) ----
        cat.band_index = 0.8 * cat.band_index + 0.2 * percentile_to_band_index(cat.percentile)
        cat.max_band_index_seen = max(cat.max_band_index_seen, band_index)

        # ---- Early easy/medium miss flag (§9.2) ----
        if (
            self._is_early_phase()
            and not was_correct
            and band_index <= EARLY_EASY_MEDIUM_MISS_MAX_BAND
        ):
            self.section.early_easy_medium_miss = True
            self.section.early_easy_wrong_count += 1

        # ---- Streaks ----
        if was_correct:
            cat.correct += 1
            cat.streak += 1
            self.section.global_correct_streak += 1
            self.section.category_correct_streak[category] += 1
        else:
            cat.streak = 0
            self.section.global_correct_streak = 0
            self.section.category_correct_streak[category] = 0

        cat.confidence = min(1.0, cat.attempts / max(1, self.section.category_quotas[category]))

        # ---- Track 2: IRT theta ----
        cat.response_history.append((b, a, c, was_correct))
        cat.theta = estimate_theta_map(cat.response_history)

        # ---- Early anchor lock ----
        self.maybe_lock_early_anchor()

    # ------------------------------------------------------------------
    # Section 11 — MSR scoring
    # ------------------------------------------------------------------

    def record_msr_response(self, category: str, children: List[Tuple[int, bool]]) -> None:
        """children: list of (difficulty_index, was_correct) for each MSR sub-question."""
        cat = self.section.categories[category]
        self.section.question_index += 1
        cat.attempts += len(children)
        cat.last_seen_at = self.section.question_index
        self.section.answered_by_category[category] += 1
        if self.section.answered_by_category[category] >= self.section.category_quotas[category]:
            self.section.exhausted_categories.add(category)

        child_percentiles: List[float] = []
        all_correct = True
        all_band5_plus = True

        for band_index, was_correct in children:
            delta = CHILD_DELTA_CORRECT[band_index] if was_correct else CHILD_DELTA_WRONG[band_index]
            child_percentiles.append(BAND_TABLE[band_index]["baseline_pct"] + delta)
            all_correct = all_correct and was_correct
            all_band5_plus = all_band5_plus and band_index >= 5

            if was_correct:
                cat.correct += 1
                cat.streak += 1
                self.section.global_correct_streak += 1
                self.section.category_correct_streak[category] += 1
            else:
                cat.streak = 0
                self.section.global_correct_streak = 0
                self.section.category_correct_streak[category] = 0

            if (
                self._is_early_phase()
                and not was_correct
                and band_index <= EARLY_EASY_MEDIUM_MISS_MAX_BAND
            ):
                self.section.early_easy_medium_miss = True
                self.section.early_easy_wrong_count += 1

            # each child is an independent IRT observation (§11.2)
            band_data = BAND_TABLE[band_index]
            cat.response_history.append((band_data["b"], band_data["a"], PSEUDO_GUESSING_C, was_correct))
            cat.max_band_index_seen = max(cat.max_band_index_seen, band_index)

        effective_delta = sum(child_percentiles) / len(child_percentiles)
        if all_correct and all_band5_plus:
            effective_delta += 2

        cat.percentile = max(0.0, min(100.0, cat.percentile + 0.7 * effective_delta))
        cat.band_index = 0.8 * cat.band_index + 0.2 * percentile_to_band_index(cat.percentile)
        cat.confidence = min(1.0, cat.attempts / max(1, self.section.category_quotas[category]))

        # overall percentile follows the same damped-delta + cap/penalty logic as §6.3,
        # applied to the blended child delta
        avg_band_index = round(sum(b for b, _ in children) / len(children))
        if all_correct:
            delta_overall = OVERALL_DELTA_DAMPING * effective_delta
            delta_overall = min(delta_overall, OVERALL_CORRECT_CAP[avg_band_index])
        else:
            penalty_table = EARLY_WRONG_PENALTY if self._is_early_phase() else LATER_WRONG_PENALTY
            delta_overall = penalty_table[avg_band_index]
        self.section.overall_percentile = max(0.0, min(100.0, self.section.overall_percentile + delta_overall))

        cat.theta = estimate_theta_map(cat.response_history)
        self.maybe_lock_early_anchor()

    def get_msr_band_cap(self, category: str) -> int:
        """Section 11.3 — early-phase MSR target band caps."""
        overall = self.section.overall_percentile
        streak = self.section.global_correct_streak
        no_early_miss = not self.section.early_easy_medium_miss

        if overall >= 80 and streak >= 4 and no_early_miss:
            return 6
        if overall >= 70 and streak >= 3 and no_early_miss:
            return 5
        if overall < 70:
            return 4

        if self.can_access_band_8(category) and self.effective_percentile() >= 85 and streak >= 4:
            return 8

        return 5

    # ------------------------------------------------------------------
    # Section 12 — Final score computation
    # ------------------------------------------------------------------

    def finalize_section(self, n_unanswered: int = 0) -> Dict[str, object]:
        section = self.section
        category_final_percentiles = {}

        for name, cat in section.categories.items():
            theta = cat.theta
            if n_unanswered > 0:
                theta = apply_unanswered_penalty(theta, n_unanswered)
            category_final_percentiles[name] = norm_cdf(theta) * 100.0

        avg_category_percentile = (
            sum(category_final_percentiles.values()) / len(category_final_percentiles)
            if category_final_percentiles else 0.0
        )

        raw_section_percentile = (
            RAW_SECTION_PERCENTILE_CATEGORY_WEIGHT * avg_category_percentile
            + RAW_SECTION_PERCENTILE_OVERALL_WEIGHT * section.overall_percentile
        )

        if section.early_easy_medium_miss:
            early_ceiling = section.early_anchor_percentile + EARLY_MISS_CEILING_BONUS
            section.section_percentile_for_scale = min(
                raw_section_percentile, max(early_ceiling, EARLY_MISS_CEILING_FLOOR)
            )
        else:
            section.section_percentile_for_scale = raw_section_percentile

        mapping = SECTION_MAPPINGS.get(section.section_name, QUANT_MAPPING)
        scaled_score = percentile_to_scaled_score(section.section_percentile_for_scale, mapping)

        if section.early_easy_medium_miss:
            scaled_score = min(scaled_score, EARLY_MISS_MAX_SCALED_SCORE)

        section.scaled_score = scaled_score

        return {
            "section_name": section.section_name,
            "scaled_score": scaled_score,
            "overall_percentile": section.overall_percentile,
            "section_percentile_for_scale": section.section_percentile_for_scale,
            "category_final_percentiles": category_final_percentiles,
            "early_easy_medium_miss": section.early_easy_medium_miss,
            "early_anchor_percentile": section.early_anchor_percentile,
            "category_theta": {name: cat.theta for name, cat in section.categories.items()},
        }


# ===========================================================================
# Convenience factory
# ===========================================================================

def new_section_state(
    section_name: str,
    category_quotas: Dict[str, int],
    total_questions: int,
    seed_overall_percentile: float = 60.0,
) -> SectionState:
    categories = {
        name: CategoryState(name=name, percentile=seed_overall_percentile)
        for name in category_quotas
    }
    return SectionState(
        section_name=section_name,
        categories=categories,
        category_quotas=dict(category_quotas),
        total_questions=total_questions,
        overall_percentile=seed_overall_percentile,
        early_anchor_percentile=seed_overall_percentile,
        section_percentile_for_scale=seed_overall_percentile,
        seed_overall_percentile=seed_overall_percentile,
    )
