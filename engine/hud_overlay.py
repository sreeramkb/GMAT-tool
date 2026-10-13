"""
engine/hud_overlay.py — JS injection for the fixed HUD overlay (architecture §13.4).

Shows question counter, a countdown timer (turns red at <=5 minutes), a
pause/resume control, and a bookmark/save-for-review toggle. Must be re-injected
on every page navigation since each question is a new URL; the timer is re-seeded
from the Python accumulator each time.
"""

from __future__ import annotations

from typing import Any, Optional

HUD_CONTAINER_ID = "gmat-sim-hud"

_INJECT_HUD_JS = """
(function() {
    const existing = document.getElementById('%(container_id)s');
    if (existing) existing.remove();

    const questionIndex = %(question_index)d;
    const totalQuestions = %(total_questions)d;
    const remainingSeconds = %(remaining_seconds)d;
    const isBookmarked = %(is_bookmarked_js)s;

    window.__gmat_review_list = window.__gmat_review_list || [];
    window.__gmat_sim_timer_state = window.__gmat_sim_timer_state || { paused: false, remaining: remainingSeconds };
    const timerState = window.__gmat_sim_timer_state;
    timerState.remaining = remainingSeconds;
    timerState.paused = !!timerState.paused;

    const hud = document.createElement('div');
    hud.id = '%(container_id)s';
    hud.style.cssText = [
        'position: fixed', 'top: 12px', 'right: 16px', 'z-index: 99999',
        'background: rgba(30,30,30,0.92)', 'color: white', 'border-radius: 8px',
        "font-family: 'Segoe UI', sans-serif", 'padding: 10px 14px',
        'font-size: 14px', 'min-width: 220px', 'box-shadow: 0 2px 8px rgba(0,0,0,0.4)'
    ].join(';');

    hud.innerHTML = `
        <div style="display:flex; justify-content:space-between; align-items:center;">
            <span>Q ${questionIndex} / ${totalQuestions}</span>
            <span id="gmat-sim-timer">-:--</span>
        </div>
        <button id="gmat-sim-pause-btn" style="margin-top:6px; width:100%%; cursor:pointer;
            background:#444; color:white; border:1px solid #666; border-radius:4px; padding:4px;">
            Pause timer
        </button>
        <button id="gmat-sim-bookmark-btn" style="margin-top:6px; width:100%%; cursor:pointer;
            background:#444; color:white; border:1px solid #666; border-radius:4px; padding:4px;">
            ${isBookmarked ? '\u2605 Saved for Review' : '\u2606 Save for Review'}
        </button>
    `;
    document.body.appendChild(hud);

    const timerEl = document.getElementById('gmat-sim-timer');
    const pauseBtn = document.getElementById('gmat-sim-pause-btn');
    let remaining = timerState.remaining;

    function render() {
        const mins = Math.floor(Math.max(remaining, 0) / 60);
        const secs = Math.max(remaining, 0) %% 60;
        timerEl.textContent = `\u23F1 ${mins}:${String(secs).padStart(2, '0')} remaining`;
        timerEl.style.color = remaining <= 300 ? '#ff5555' : 'white';
    }
    render();

    function togglePause() {
        timerState.paused = !timerState.paused;
        pauseBtn.textContent = timerState.paused ? 'Resume timer' : 'Pause timer';
        pauseBtn.style.background = timerState.paused ? '#af7a00' : '#444';
    }

    pauseBtn.addEventListener('click', () => {
        togglePause();
    });

    if (window.__gmat_hud_interval) clearInterval(window.__gmat_hud_interval);
    window.__gmat_hud_interval = setInterval(() => {
        if (!timerState.paused) {
            remaining -= 1;
            timerState.remaining = remaining;
        }
        render();
        if (remaining <= 0) clearInterval(window.__gmat_hud_interval);
    }, 1000);

    const questionId = %(question_id_js)s;
    document.getElementById('gmat-sim-bookmark-btn').addEventListener('click', () => {
        const idx = window.__gmat_review_list.indexOf(questionId);
        const btn = document.getElementById('gmat-sim-bookmark-btn');
        if (idx === -1) {
            window.__gmat_review_list.push(questionId);
            btn.textContent = '\u2605 Saved for Review';
        } else {
            window.__gmat_review_list.splice(idx, 1);
            btn.textContent = '\u2606 Save for Review';
        }
    });
})();
"""


def _js_string_literal(value: Optional[str]) -> str:
    if value is None:
        return "null"
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def build_hud_injection_script(
    question_index: int,
    total_questions: int,
    remaining_seconds: int,
    question_id: Optional[str] = None,
    is_bookmarked: bool = False,
) -> str:
    """Build the JS source that injects/re-injects the HUD overlay for the current question."""
    return _INJECT_HUD_JS % {
        "container_id": HUD_CONTAINER_ID,
        "question_index": question_index,
        "total_questions": total_questions,
        "remaining_seconds": int(remaining_seconds),
        "question_id_js": _js_string_literal(question_id),
        "is_bookmarked_js": "true" if is_bookmarked else "false",
    }


def inject_hud(
    driver: Any,
    question_index: int,
    total_questions: int,
    remaining_seconds: int,
    question_id: Optional[str] = None,
    is_bookmarked: bool = False,
) -> None:
    """Inject (or re-inject) the HUD overlay. Call this after every page navigation."""
    script = build_hud_injection_script(
        question_index, total_questions, remaining_seconds, question_id, is_bookmarked
    )
    driver.execute_script(script)


def read_review_list(driver: Any) -> list:
    """Read the bookmarked question IDs accumulated in window.__gmat_review_list."""
    return driver.execute_script("return window.__gmat_review_list || [];")


def set_timer_paused(driver: Any, paused: bool) -> bool:
    """Pause or resume the HUD countdown timer and return the resulting state."""
    return bool(driver.execute_script(
        """
        (function() {
            const state = window.__gmat_sim_timer_state || { paused: false };
            state.paused = !!arguments[0];
            window.__gmat_sim_timer_state = state;
            const btn = document.getElementById('gmat-sim-pause-btn');
            if (btn) {
                btn.textContent = state.paused ? 'Resume timer' : 'Pause timer';
                btn.style.background = state.paused ? '#af7a00' : '#444';
            }
            return state.paused;
        })();
        """,
        paused,
    ))


def read_timer_pause_state(driver: Any) -> bool:
    """Return whether the HUD timer has been paused."""
    return bool(
        driver.execute_script(
            "return !!(window.__gmat_sim_timer_state && window.__gmat_sim_timer_state.paused);"
        )
    )
