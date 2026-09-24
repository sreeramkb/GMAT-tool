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
    const label = (wrap.querySelector('.answerType, .answerLabel, .choiceLabel') || {}).textContent;
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
window.__gmat_user_choice = window.__gmat_user_choice || null;
window.__gmat_question_key = window.__gmat_question_key || window.location.href;
window.__gmat_question_started_at = window.__gmat_question_started_at || Date.now();

const currentQuestionKey = () => window.location.href;
const setCurrentQuestionContext = () => {
    window.__gmat_question_key = currentQuestionKey();
    window.__gmat_question_started_at = Date.now();
    window.__gmat_result = null;
    window.__gmat_user_choice = null;
};

setCurrentQuestionContext();

if (typeof window.timer_answer === 'function' && !window.__gmat_sim_patched) {
    const original = window.timer_answer;
    window.timer_answer = function(choiceCode) {
        if (window.__gmat_question_key !== currentQuestionKey()) return original.apply(this, arguments);
        const norm = normalizeChoice(choiceCode);
        if (!norm) return original.apply(this, arguments);
        window.__gmat_user_choice = norm;
        const result = original.apply(this, arguments);
        window.__gmat_result = {
            correct_choice: null,
            selected_choice: norm,
            was_correct: null,
        };
        return result;
    };
    window.__gmat_sim_patched = true;
}

const normalizeChoice = (value) => {
    if (!value) return null;
    const s = String(value).trim();
    const upper = s.toUpperCase();

    if (/^[A-E]$/.test(upper)) return upper;

    const singleNumber = upper.match(/^([1-5])$/);
    if (singleNumber) return ['A', 'B', 'C', 'D', 'E'][Number(singleNumber[1]) - 1];

    // GMAT Club's real timer_answer(choiceCode) uses an offset-by-10 numeric
    // code (11-15) rather than a plain 1-5 index or letter.
    const offsetNumber = upper.match(/^1([1-5])$/);
    if (offsetNumber) return ['A', 'B', 'C', 'D', 'E'][Number(offsetNumber[1]) - 1];

    const flat = upper.replace(/[^0-9]/g, '');
    if (/^[1-5]$/.test(flat)) return ['A', 'B', 'C', 'D', 'E'][Number(flat) - 1];

    return null;
};

const extractChoiceFromElement = (el) => {
    if (!el) return null;
    // A dedicated label sub-element (if present) is the least ambiguous source -
    // prefer it before falling back to the element's own (possibly noisy) text.
    const labelEl = el.querySelector && el.querySelector('.answerType, .answerLabel, .choiceLabel, [class*="answerLabel" i], [class*="choiceLabel" i]');
    if (labelEl) {
        const fromLabel = normalizeChoice(labelEl.textContent);
        if (fromLabel) return fromLabel;
    }
    const candidates = [
        el.getAttribute('data-answer'),
        el.getAttribute('data-choice'),
        el.getAttribute('value'),
        el.getAttribute('id'),
        el.getAttribute('name'),
        el.getAttribute('aria-label'),
        el.textContent,
        el.innerText,
        el.dataset && el.dataset.answer,
        el.dataset && el.dataset.choice,
    ];
    for (const candidate of candidates) {
        const normalized = normalizeChoice(candidate);
        if (normalized) return normalized;
    }
    // Last resort: text like "A." / "A)" / "A Correct" where a letter prefix is
    // followed by other markup/labels (e.g. a checkmark icon's text) that would
    // otherwise fail a strict exact-match normalize.
    const text = ((el.textContent || el.innerText || '') + '').trim();
    const leadingMatch = text.match(/^([A-E])(?=[.\):\s]|$)/i);
    if (leadingMatch) return leadingMatch[1].toUpperCase();
    return null;
};

const getPageCorrectAnswer = () => {
    // Ordered from most-specific (least likely to false-positive) to broadest,
    // since the first normalize-able match wins.
    const selectors = [
        '.correctAnswer', '.correct-answer', '.correctAnswerBlock', '.answer-key',
        '[data-correct="true"]',
        '.timerResult', '.result', '.answer-correct', '[aria-label*="correct"]',
        '[class*="correct"]', '[id*="correct"]'
    ];
    const values = [];
    for (const selector of selectors) {
        for (const el of document.querySelectorAll(selector)) {
            const norm = normalizeChoice(
                el.getAttribute('data-answer') ||
                el.getAttribute('data-choice') ||
                el.textContent ||
                el.innerText
            );
            if (norm) values.push(norm);
        }
    }
    return values.find(Boolean) || null;
};

// GMAT Club replaces the answer buttons with a per-choice stats display once
// submitted: each choice becomes a ".statisticWrap" wrapper containing a
// ".answerType" letter (a-e). The WRAPPER itself gets "correctAnswer" and/or
// "selectedAnswer" classes added - but every child ".answerPercentage" span
// always carries a "correctAnswer" class too (it's just a bar-color styling
// class, unrelated to which choice is actually correct), so correctness must
// be read from the wrapper's own classList, never a descendant's.
const getRevealedVerdict = () => {
    let correct = null;
    let selected = null;
    for (const wrap of document.querySelectorAll('.statisticWrap, .statisticWrapExisting')) {
        const typeEl = wrap.querySelector('.answerType');
        if (!typeEl) continue;
        const letter = normalizeChoice(typeEl.textContent);
        if (!letter) continue;
        if (wrap.classList.contains('correctAnswer')) correct = letter;
        if (wrap.classList.contains('selectedAnswer')) selected = letter;
    }
    return { correct, selected };
};

const isAnswerLike = (el) => !!(el && el.closest && extractChoiceFromElement(el));

const target = document.querySelector('#timer_abcde, .timer_abcde, .question-answers, .answer-options, .answers, .option-list, .choice-list');
if (target && !window.__gmat_sim_observer) {
    const persistChoice = (el) => {
        if (!el || window.__gmat_question_key !== currentQuestionKey()) return;
        const chosen = normalizeChoice(extractChoiceFromElement(el));
        if (!chosen || !/^[A-E]$/.test(chosen)) return;
        window.__gmat_user_choice = chosen;
        window.__gmat_result = {
            correct_choice: null,
            selected_choice: chosen,
            was_correct: null,
        };
    };

    target.addEventListener('pointerdown', (event) => {
        const el = event.target.closest('button, li, label, span, div');
        if (isAnswerLike(el)) persistChoice(el);
    }, true);

    target.addEventListener('click', (event) => {
        const el = event.target.closest('button, li, label, span, div');
        if (isAnswerLike(el)) persistChoice(el);
    }, true);

    const observer = new MutationObserver(() => {
        if (window.__gmat_question_key !== currentQuestionKey()) {
            setCurrentQuestionContext();
            return;
        }

        // The click handler above already captured exactly which option the user
        // pressed. Trust that capture for "chosen" - the revealed ".selectedAnswer"
        // wrapper is a secondary confirmation, not the primary source, since a
        // future markup change there shouldn't silently break selection tracking.
        const chosen = window.__gmat_user_choice;
        if (!chosen) return;

        const { correct } = getRevealedVerdict();
        const right = correct || getPageCorrectAnswer();

        if (right) {
            window.__gmat_result = {
                correct_choice: right,
                selected_choice: chosen,
                was_correct: chosen === right,
            };
        } else {
            window.__gmat_result = {
                correct_choice: null,
                selected_choice: chosen,
                was_correct: null,
            };
        }
    });

    observer.observe(target, { childList: true, subtree: true, attributes: true, attributeFilter: ['class', 'id', 'data-answer', 'data-choice', 'value', 'aria-pressed', 'aria-selected'] });
    window.__gmat_sim_observer = observer;
}
"""



def install_submission_interceptor(driver: Any) -> None:
    """Monkey-patch timer_answer() and attach a MutationObserver to capture correctness."""
    driver.execute_script(_INSTALL_SUBMISSION_INTERCEPTOR_JS)


def set_question_context(driver: Any, question_url: str) -> None:
    """Reset browser-side answer state for a newly loaded GMAT Club question URL."""
    driver.execute_script(
        """
        window.__gmat_question_key = arguments[0] || window.location.href;
        window.__gmat_user_choice = null;
        window.__gmat_result = null;
        window.__gmat_question_started_at = Date.now();
        """,
        question_url,
    )


def read_submission_result(driver: Any) -> Optional[Dict[str, Any]]:
    """Read window.__gmat_result populated by the submission interceptor, if available."""
    return driver.execute_script("return window.__gmat_result || null;")


# ---------------------------------------------------------------------------
# Diagnostics — dump the real answer-area markup so brittle selectors above
# can be corrected against GMAT Club's actual DOM instead of guessed again.
# ---------------------------------------------------------------------------

_DUMP_ANSWER_AREA_JS = """
const candidates = [
    '#timer_abcde', '.timer_abcde', '.question-answers', '.answer-options',
    '.answers', '.option-list', '.choice-list'
];
let target = null;
let matchedSelector = null;
for (const sel of candidates) {
    const el = document.querySelector(sel);
    if (el) { target = el; matchedSelector = sel; break; }
}

// None of the guessed selectors matched GMAT Club's real markup - fall back to
// a heuristic: find leaf elements whose text is exactly one of A-E and see if
// several of them share a common ancestor (that ancestor is likely the answer list).
let letterElementCount = 0;
if (!target) {
    const letterEls = [...document.querySelectorAll('button, li, label, span, div')].filter(el => {
        if (el.children.length > 0) return false;
        return /^[A-E]$/.test((el.textContent || '').trim());
    });
    letterElementCount = letterEls.length;
    const parentCounts = new Map();
    for (const el of letterEls) {
        const p = el.parentElement && el.parentElement.parentElement;
        if (!p) continue;
        parentCounts.set(p, (parentCounts.get(p) || 0) + 1);
    }
    let bestParent = null, bestCount = 0;
    for (const [p, c] of parentCounts.entries()) {
        if (c > bestCount) { bestCount = c; bestParent = p; }
    }
    if (bestParent && bestCount >= 3) {
        target = bestParent;
        matchedSelector = '(heuristic: common ancestor of single-letter elements)';
    }
}

const html = target ? target.outerHTML : null;
return {
    matched_selector: matchedSelector,
    target_html: html ? html.slice(0, 20000) : null,
    letter_element_count: letterElementCount,
    timer_answer_source: (typeof window.timer_answer === 'function') ? window.timer_answer.toString() : null,
    body_html_length: document.body ? document.body.innerHTML.length : 0,
};
"""


def dump_answer_area_diagnostics(driver: Any) -> Dict[str, Any]:
    """Capture the real answer-choices markup + timer_answer() source for debugging selectors."""
    return driver.execute_script(f"return (function() {{ {_DUMP_ANSWER_AREA_JS} }})();")
