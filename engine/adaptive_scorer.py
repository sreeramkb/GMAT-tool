"""
engine/adaptive_scorer.py — Pure IRT math functions (V1 engine primitives).

These are the standalone 3PL/MAP building blocks used by the simpler IRT-only
scoring mode. They are independent of the V3 hybrid engine's band tables so
they can be reused with any (b, a, c) item parameters.
"""

from __future__ import annotations

import math
from typing import Sequence, Tuple

try:
    from scipy.optimize import minimize_scalar
    _HAVE_SCIPY = True
except ImportError:  # pragma: no cover - fallback path
    _HAVE_SCIPY = False

THETA_MIN, THETA_MAX = -3.5, 3.5
DEFAULT_PSEUDO_GUESSING_C = 0.20

# (b, a, c, was_correct)
ResponseTuple = Tuple[float, float, float, bool]


def p_correct(theta: float, b: float, a: float, c: float = DEFAULT_PSEUDO_GUESSING_C) -> float:
    """3PL probability of a correct response."""
    exponent = -1.7 * a * (theta - b)
    return c + (1.0 - c) / (1.0 + math.exp(exponent))


def log_posterior(theta: float, history: Sequence[ResponseTuple], sigma: float = 1.5) -> float:
    """Log-posterior = log-likelihood of the response history + log N(0, sigma^2) prior."""
    log_likelihood = 0.0
    for b, a, c, was_correct in history:
        p = p_correct(theta, b, a, c)
        p = min(max(p, 1e-9), 1 - 1e-9)
        log_likelihood += math.log(p) if was_correct else math.log(1 - p)
    log_prior = -(theta ** 2) / (2 * sigma ** 2)
    return log_likelihood + log_prior


def estimate_theta_map(history: Sequence[ResponseTuple]) -> float:
    """MAP estimation of theta, with sigma=1.5 for <=10 responses, 2.0 otherwise."""
    if not history:
        return 0.0

    sigma = 1.5 if len(history) <= 10 else 2.0

    if _HAVE_SCIPY:
        result = minimize_scalar(
            lambda theta: -log_posterior(theta, history, sigma),
            bounds=(THETA_MIN, THETA_MAX),
            method="bounded",
        )
        return float(min(max(result.x, THETA_MIN), THETA_MAX))

    # Grid-search fallback (step 0.1) for environments without scipy
    best_theta, best_lp = 0.0, -math.inf
    theta = THETA_MIN
    while theta <= THETA_MAX + 1e-9:
        lp = log_posterior(theta, history, sigma)
        if lp > best_lp:
            best_lp, best_theta = lp, theta
        theta += 0.1
    return best_theta


def fisher_information(theta: float, b: float, a: float, c: float = DEFAULT_PSEUDO_GUESSING_C) -> float:
    """3PL Fisher information (omits the 1.7^2 scaling constant; ranking-only use)."""
    p = p_correct(theta, b, a, c)
    p = min(max(p, 1e-9), 1 - 1e-9)
    return (a ** 2) * ((p - c) ** 2 / (1 - c) ** 2) * ((1 - p) / p)


def standard_error(theta: float, history: Sequence[ResponseTuple]) -> float:
    """SE(theta) = 1 / sqrt(sum of Fisher information across all responses)."""
    total_info = sum(fisher_information(theta, b, a, c) for b, a, c, _ in history)
    if total_info <= 0:
        return float("inf")
    return 1.0 / math.sqrt(total_info)


def theta_to_score(theta: float, score_min: int = 60, score_max: int = 90) -> int:
    """Linear mapping of theta in [-3.5, 3.5] to a scaled score in [score_min, score_max]."""
    theta = min(max(theta, THETA_MIN), THETA_MAX)
    frac = (theta - THETA_MIN) / (THETA_MAX - THETA_MIN)
    return round(score_min + frac * (score_max - score_min))


def unanswered_penalty(theta: float, n_unanswered: int, penalty_per_item: float = 0.4) -> float:
    """Clamp theta after subtracting a fixed penalty per unanswered item."""
    penalized = theta - penalty_per_item * n_unanswered
    return min(max(penalized, THETA_MIN), THETA_MAX)
