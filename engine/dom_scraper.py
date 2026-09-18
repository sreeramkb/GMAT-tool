"""
engine/dom_scraper.py — JS injection helpers for scraping GMAT Club question
statistics and reading the in-page timer (architecture §13.2, §13.5, §14.2).

Functions take a Selenium-like `driver` object (anything exposing
`execute_script`) so they work with both plain Selenium and
undetected_chromedriver without a hard import dependency.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

# ---------------------------------------------------------------------------
# §13.2 — CSS feedback masking
# ---------------------------------------------------------------------------

FEEDBACK_MASK_CSS = """
.timerBoxTop, .timerResult, .correctAnswerBlock, .showAnswerMessage,
.answersPopup, .timerResultLeft, .timerResultRight, .answer-block,
.solution, .explanation, .solution-container, .answer-explanation,
.forum-post-solution, .tag, .tags, .topic-tags, .difficulty-tag,
a[href*='difficulty' i] {
    opacity: 0 !important;
    pointer-events: none !important;
    position: absolute !important;
    left: -9999px !important;
}

.statisticWrap:has(.answerPercentage),
.statisticWrapExisting:has(.answerPercentage) {
    opacity: 0 !important;
    pointer-events: none !important;
    position: absolute !important;
    left: -9999px !important;
}
"""

_INJECT_MASK_CSS_JS = """
const style = document.createElement('style');
style.id = 'gmat-sim-feedback-mask';
style.textContent = arguments[0];
document.head.appendChild(style);
"""

# Difficulty labels aren't always caught by CSS class selectors, so also hide
# by text content.
_HIDE_DIFFICULTY_TEXT_JS = """
const patterns = [/sub\\s*505/i, /505\\s*-\\s*555/, /555\\s*-\\s*605/,
                   /605\\s*-\\s*655/, /655\\s*-\\s*705/, /705\\s*-\\s*805/, /805\\s*\\+/];
document.querySelectorAll('a, span, div').forEach(el => {
    const text = el.textContent || '';
    if (patterns.some(p => p.test(text)) && el.children.length === 0) {
        el.style.opacity = '0';
        el.style.pointerEvents = 'none';
        el.style.position = 'absolute';
        el.style.left = '-9999px';
    }
});
"""


def inject_feedback_mask(driver: Any) -> None:
    """Inject CSS + JS to hide correctness feedback, solutions, and difficulty tags."""
    driver.execute_script(_INJECT_MASK_CSS_JS, FEEDBACK_MASK_CSS)
    driver.execute_script(_HIDE_DIFFICULTY_TEXT_JS)


# ---------------------------------------------------------------------------
# §13.5 — DOM data scraping (diagnostics only, not used for scoring)
# ---------------------------------------------------------------------------

_SCRAPE_STATS_JS = """
function pct(selector) {
    const el = document.querySelector(selector);
    if (!el) return null;
    const match = (el.textContent || '').match(/([\\d.]+)\\s*%/);
    return match ? parseFloat(match[1]) : null;
}
function text(selector) {
    const el = document.querySelector(selector);
    return el ? (el.textContent || '').trim() : null;
}

const result = {
    difficulty_percentage: pct('.difficultyPercentage, .difficulty-value'),
    correct_percentage: pct('.correctPercentage, .statisticWrap .correct .answerPercentage'),
    wrong_percentage: pct('.wrongPercentage, .statisticWrap .wrong .answerPercentage'),
    avg_time_correct: text('.avgTimeCorrect, .correctAvgTime'),
    avg_time_wrong: text('.avgTimeWrong, .wrongAvgTime'),
    sample_size: text('.sampleSize, .questionStatsCount'),
    answer_distribution: {}
};

document.querySelectorAll('.statisticWrap, .statisticWrapExisting').forEach(wrap => {
    const label = (wrap.querySelector('.answerLabel, .choiceLabel') || {}).textContent;
    const value = pct.call(null, null) || null;
    const pctEl = wrap.querySelector('.answerPercentage');
    if (label && pctEl) {
        const match = pctEl.textContent.match(/([\\d.]+)\\s*%/);
        if (match) {
            result.answer_distribution[label.trim()] = parseFloat(match[1]);
        }
    }
});

return result;
"""


def scrape_question_stats(driver: Any) -> Dict[str, Any]:
    """Scrape difficulty %, correct/wrong %, avg times, sample size, and answer distribution."""
    return driver.execute_script(f"return (function() {{ {_SCRAPE_STATS_JS} }})();")


# ---------------------------------------------------------------------------
# §14.1 / §14.2 — DOM timer reading & auto-start
# ---------------------------------------------------------------------------

_READ_TIMER_SECONDS_JS = """
const el = document.querySelector('#timer_time, .timerTime, .timer-display');
if (!el) return null;
const parts = (el.textContent || '').trim().split(':').map(Number);
if (parts.some(isNaN)) return null;
if (parts.length === 3) return parts[0] * 3600 + parts[1] * 60 + parts[2];
if (parts.length === 2) return parts[0] * 60 + parts[1];
return parts[0] || null;
"""

_AUTO_START_TIMER_JS = """
const playBtn = document.querySelector('#timer_play, .timerPlayButton, .timer-start');
if (playBtn) {
    playBtn.click();
    return true;
}
return false;
"""


def read_dom_timer_seconds(driver: Any) -> Optional[float]:
    """Read the per-question elapsed time (seconds) from the GMAT Club DOM timer."""
    return driver.execute_script(f"return (function() {{ {_READ_TIMER_SECONDS_JS} }})();")


def auto_start_timer(driver: Any) -> bool:
    """Click the GMAT Club timer play button so thinking-time timing begins."""
    return bool(driver.execute_script(f"return (function() {{ {_AUTO_START_TIMER_JS} }})();"))


# ---------------------------------------------------------------------------
# §13.3 — Submission interceptor (monkey-patch timer_answer + MutationObserver)
# ---------------------------------------------------------------------------

_INSTALL_SUBMISSION_INTERCEPTOR_JS = """
window.__gmat_result = window.__gmat_result || null;

if (typeof window.timer_answer === 'function' && !window.__gmat_sim_patched) {
    const original = window.timer_answer;
    window.timer_answer = function(choiceCode) {
        window.__gmat_user_choice = choiceCode;
        const result = original.apply(this, arguments);
        const optionsContainer = document.querySelector('#timer_abcde');
        if (optionsContainer) {
            optionsContainer.style.opacity = '0';
            optionsContainer.style.pointerEvents = 'none';
        }
        return result;
    };
    window.__gmat_sim_patched = true;
}

const target = document.querySelector('#timer_abcde');
if (target && !window.__gmat_sim_observer) {
    const observer = new MutationObserver(() => {
        const correctEl = target.querySelector('.correctAnswer');
        const selectedEl = target.querySelector('.selectedAnswer');
        if (correctEl && selectedEl) {
            window.__gmat_result = {
                correct_choice: correctEl.textContent.trim(),
                selected_choice: selectedEl.textContent.trim(),
                was_correct: correctEl === selectedEl || correctEl.isEqualNode(selectedEl),
            };
        }
    });
    observer.observe(target, { childList: true, subtree: true, attributes: true });
    window.__gmat_sim_observer = observer;
}
"""


def install_submission_interceptor(driver: Any) -> None:
    """Monkey-patch timer_answer() and attach a MutationObserver to capture correctness."""
    driver.execute_script(_INSTALL_SUBMISSION_INTERCEPTOR_JS)


def read_submission_result(driver: Any) -> Optional[Dict[str, Any]]:
    """Read window.__gmat_result populated by the submission interceptor, if available."""
    return driver.execute_script("return window.__gmat_result || null;")
