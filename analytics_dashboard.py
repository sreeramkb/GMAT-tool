"""
analytics_dashboard.py — Generates analytics_report.html from all session
JSON reports across quant_reports/, di_reports/, and verbal_reports/
(architecture §17).

Reads every per-session JSON report, normalizes them into a common schema,
and renders a single self-contained HTML file (inline CSS/JS, no external
dependencies) with a session selector, summary stats, a theta/percentile
trajectory chart drawn on <canvas>, subcategory performance bars, and a
sortable question-level detail table.
"""

from __future__ import annotations

import json
import webbrowser
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT_DIR = Path(__file__).resolve().parent

REPORT_DIRS: Dict[str, Path] = {
    "Quant": ROOT_DIR / "Quant" / "quant_reports",
    "DI": ROOT_DIR / "DI_Adaptive" / "di_reports",
    "Verbal": ROOT_DIR / "Verbal" / "verbal_reports",
}

OUTPUT_PATH = ROOT_DIR / "analytics_report.html"


# ===========================================================================
# Normalization
# ===========================================================================

def normalize_session(raw: dict, section: str, file_path: Path) -> Optional[Dict[str, Any]]:
    """Convert a raw per-session report JSON into the common dashboard schema."""
    final_result = raw.get("final_result", {})
    diagnostics_log = raw.get("diagnostics_log", [])

    if not diagnostics_log and not final_result:
        return None

    accuracy_by_category: Dict[str, float] = {}
    totals: Dict[str, int] = {}
    corrects: Dict[str, int] = {}
    for entry in diagnostics_log:
        cat = entry.get("category", "Unknown")
        totals[cat] = totals.get(cat, 0) + 1
        if entry.get("was_correct"):
            corrects[cat] = corrects.get(cat, 0) + 1
    for cat, total in totals.items():
        accuracy_by_category[cat] = round(corrects.get(cat, 0) / total, 3) if total else 0.0

    total_answered = len(diagnostics_log)
    total_correct = sum(1 for e in diagnostics_log if e.get("was_correct"))
    accuracy = round(total_correct / total_answered, 3) if total_answered else 0.0

    questions = [
        {
            "index": i + 1,
            "category": entry.get("category", "Unknown"),
            "band": entry.get("difficulty_index"),
            "correct": bool(entry.get("was_correct")),
            "elapsed_seconds": entry.get("elapsed_seconds"),
            "theta_after": entry.get("theta_after"),
            "overall_percentile_after": entry.get("overall_percentile_after"),
            "url": entry.get("url"),
        }
        for i, entry in enumerate(diagnostics_log)
    ]

    return {
        "session_id": raw.get("session_id", file_path.stem),
        "section": raw.get("section", section),
        "timestamp": file_path.stat().st_mtime,
        "scaled_score": final_result.get("scaled_score"),
        "section_percentile": final_result.get("section_percentile_for_scale"),
        "overall_percentile": final_result.get("overall_percentile"),
        "early_easy_medium_miss": final_result.get("early_easy_medium_miss", False),
        "accuracy": accuracy,
        "time_used_seconds": raw.get("elapsed_thinking_seconds"),
        "accuracy_by_category": accuracy_by_category,
        "theta_trajectory": [q["theta_after"] for q in questions],
        "percentile_trajectory": [q["overall_percentile_after"] for q in questions],
        "questions": questions,
    }


def load_sessions() -> List[Dict[str, Any]]:
    sessions: List[Dict[str, Any]] = []
    for section, directory in REPORT_DIRS.items():
        if not directory.exists():
            continue
        for file_path in sorted(directory.glob("*.json")):
            try:
                raw = json.loads(file_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            normalized = normalize_session(raw, section, file_path)
            if normalized:
                sessions.append(normalized)

    sessions.sort(key=lambda s: s["timestamp"], reverse=True)
    return sessions


# ===========================================================================
# HTML generation
# ===========================================================================

_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>GMAT Simulator — Analytics Dashboard</title>
<style>
  body { font-family: 'Segoe UI', sans-serif; background: #1b1b1f; color: #eee; margin: 0; padding: 24px; }
  h1 { font-size: 20px; margin-bottom: 4px; }
  .subtitle { color: #999; margin-bottom: 20px; }
  select, button { background: #2a2a30; color: #eee; border: 1px solid #444; border-radius: 6px;
                   padding: 6px 10px; font-size: 14px; }
  .panel { background: #232328; border-radius: 10px; padding: 16px 20px; margin-bottom: 20px;
           box-shadow: 0 1px 4px rgba(0,0,0,0.4); }
  .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 12px; }
  .stat { background: #2a2a30; border-radius: 8px; padding: 10px 14px; }
  .stat .label { color: #999; font-size: 12px; text-transform: uppercase; }
  .stat .value { font-size: 22px; font-weight: 600; margin-top: 4px; }
  canvas { background: #16161a; border-radius: 8px; }
  .bar-row { display: flex; align-items: center; margin: 6px 0; }
  .bar-label { width: 260px; font-size: 13px; color: #ccc; }
  .bar-track { flex: 1; background: #16161a; border-radius: 4px; height: 16px; overflow: hidden; }
  .bar-fill { height: 100%; background: linear-gradient(90deg, #4f9dff, #6ee7b7); }
  .bar-value { width: 50px; text-align: right; font-size: 12px; color: #ccc; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: left; padding: 6px 10px; border-bottom: 1px solid #333; }
  th { cursor: pointer; color: #9ecbff; user-select: none; }
  th.sorted-asc::after { content: ' \\25B2'; }
  th.sorted-desc::after { content: ' \\25BC'; }
  tr.correct td.correct-cell { color: #6ee7b7; }
  tr.incorrect td.correct-cell { color: #ff8080; }
  a { color: #9ecbff; }
  .empty-state { color: #999; padding: 40px; text-align: center; }
</style>
</head>
<body>

<h1>GMAT Simulator — Analytics Dashboard</h1>
<div class="subtitle">Diagnostic report, not an official score prediction.</div>

<div class="panel" id="selector-panel">
  <label for="session-select">Session:</label>
  <select id="session-select"></select>
</div>

<div id="dashboard-content"></div>

<script>
const SESSIONS = __SESSIONS_JSON__;

const contentEl = document.getElementById('dashboard-content');
const selectEl = document.getElementById('session-select');

function fmtTimestamp(ts) {
    const d = new Date(ts * 1000);
    return d.toLocaleString();
}

function populateSelector() {
    if (SESSIONS.length === 0) {
        document.getElementById('selector-panel').style.display = 'none';
        contentEl.innerHTML = '<div class="empty-state">No session reports found yet. Complete a section to see analytics here.</div>';
        return;
    }
    SESSIONS.forEach((s, i) => {
        const opt = document.createElement('option');
        opt.value = i;
        opt.textContent = `${s.section} — ${fmtTimestamp(s.timestamp)} — Score: ${s.scaled_score ?? 'N/A'}`;
        selectEl.appendChild(opt);
    });
    selectEl.addEventListener('change', () => renderSession(SESSIONS[selectEl.value]));
    renderSession(SESSIONS[0]);
}

function renderSummary(session) {
    const stats = [
        ['Section', session.section],
        ['Scaled Score', session.scaled_score ?? 'N/A'],
        ['Section Percentile', session.section_percentile != null ? session.section_percentile.toFixed(1) : 'N/A'],
        ['Accuracy', (session.accuracy * 100).toFixed(1) + '%'],
        ['Time Used', session.time_used_seconds != null ? Math.round(session.time_used_seconds / 60) + ' min' : 'N/A'],
        ['Questions', session.questions.length],
    ];
    if (session.early_easy_medium_miss) {
        stats.push(['Early Miss Flag', 'Yes (score capped)']);
    }
    return `
      <div class="panel">
        <div class="stats-grid">
          ${stats.map(([label, value]) => `
            <div class="stat">
              <div class="label">${label}</div>
              <div class="value">${value}</div>
            </div>`).join('')}
        </div>
      </div>`;
}

function drawTrajectoryChart(canvas, theta, percentile) {
    const ctx = canvas.getContext('2d');
    const w = canvas.width, h = canvas.height;
    ctx.clearRect(0, 0, w, h);

    const padding = 36;
    const n = Math.max(theta.length, 1);
    const xStep = (w - padding * 2) / Math.max(n - 1, 1);

    function plot(series, color, minV, maxV) {
        ctx.beginPath();
        ctx.strokeStyle = color;
        ctx.lineWidth = 2;
        series.forEach((v, i) => {
            if (v == null) return;
            const x = padding + i * xStep;
            const y = h - padding - ((v - minV) / (maxV - minV || 1)) * (h - padding * 2);
            if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
        });
        ctx.stroke();
    }

    // axes
    ctx.strokeStyle = '#444';
    ctx.beginPath();
    ctx.moveTo(padding, padding); ctx.lineTo(padding, h - padding); ctx.lineTo(w - padding, h - padding);
    ctx.stroke();

    plot(theta, '#4f9dff', -3.5, 3.5);
    plot(percentile, '#6ee7b7', 0, 100);

    ctx.fillStyle = '#4f9dff';
    ctx.fillText('theta (per-category)', padding, 16);
    ctx.fillStyle = '#6ee7b7';
    ctx.fillText('overall percentile', padding + 160, 16);
}

function renderChart(session) {
    return `
      <div class="panel">
        <h3 style="margin-top:0;">Theta / Percentile Trajectory</h3>
        <canvas id="trajectory-canvas" width="900" height="260"></canvas>
      </div>`;
}

function renderCategoryBars(session) {
    const entries = Object.entries(session.accuracy_by_category);
    if (entries.length === 0) return '';
    return `
      <div class="panel">
        <h3 style="margin-top:0;">Subcategory Performance</h3>
        ${entries.map(([cat, acc]) => `
          <div class="bar-row">
            <div class="bar-label">${cat}</div>
            <div class="bar-track"><div class="bar-fill" style="width:${(acc * 100).toFixed(0)}%"></div></div>
            <div class="bar-value">${(acc * 100).toFixed(0)}%</div>
          </div>`).join('')}
      </div>`;
}

let currentSort = { key: 'index', asc: true };

function renderTable(session) {
    const cols = [
        ['index', 'Q#'], ['category', 'Category'], ['band', 'Band'],
        ['correct', 'Correct'], ['elapsed_seconds', 'Time (s)'],
        ['theta_after', 'Theta'], ['overall_percentile_after', 'Percentile'],
    ];
    const rows = sortedQuestions(session);
    return `
      <div class="panel">
        <h3 style="margin-top:0;">Question Detail</h3>
        <table id="question-table">
          <thead><tr>
            ${cols.map(([key, label]) => `<th data-key="${key}" class="${currentSort.key === key ? (currentSort.asc ? 'sorted-asc' : 'sorted-desc') : ''}">${label}</th>`).join('')}
            <th>Link</th>
          </tr></thead>
          <tbody>
            ${rows.map(q => `
              <tr class="${q.correct ? 'correct' : 'incorrect'}">
                <td>${q.index}</td>
                <td>${q.category}</td>
                <td>${q.band ?? ''}</td>
                <td class="correct-cell">${q.correct ? 'Yes' : 'No'}</td>
                <td>${q.elapsed_seconds != null ? Math.round(q.elapsed_seconds) : ''}</td>
                <td>${q.theta_after != null ? q.theta_after.toFixed(2) : ''}</td>
                <td>${q.overall_percentile_after != null ? q.overall_percentile_after.toFixed(1) : ''}</td>
                <td>${q.url ? `<a href="${q.url}" target="_blank">open</a>` : ''}</td>
              </tr>`).join('')}
          </tbody>
        </table>
      </div>`;
}

function sortedQuestions(session) {
    const rows = [...session.questions];
    const { key, asc } = currentSort;
    rows.sort((a, b) => {
        let av = a[key], bv = b[key];
        if (typeof av === 'string') { av = av.toLowerCase(); bv = (bv || '').toLowerCase(); }
        if (av == null) av = -Infinity;
        if (bv == null) bv = -Infinity;
        if (av < bv) return asc ? -1 : 1;
        if (av > bv) return asc ? 1 : -1;
        return 0;
    });
    return rows;
}

function attachSortHandlers(session) {
    document.querySelectorAll('#question-table th[data-key]').forEach(th => {
        th.addEventListener('click', () => {
            const key = th.dataset.key;
            if (currentSort.key === key) currentSort.asc = !currentSort.asc;
            else currentSort = { key, asc: true };
            renderSession(session);
        });
    });
}

function renderSession(session) {
    contentEl.innerHTML = renderSummary(session) + renderChart(session) + renderCategoryBars(session) + renderTable(session);
    const canvas = document.getElementById('trajectory-canvas');
    if (canvas) drawTrajectoryChart(canvas, session.theta_trajectory, session.percentile_trajectory);
    attachSortHandlers(session);
}

populateSelector();
</script>
</body>
</html>
"""


def render_html(sessions: List[Dict[str, Any]]) -> str:
    sessions_json = json.dumps(sessions, default=str).replace("</script", "<\\/script")
    return _HTML_TEMPLATE.replace("__SESSIONS_JSON__", sessions_json)


def generate_dashboard(open_in_browser: bool = True) -> Path:
    sessions = load_sessions()
    html = render_html(sessions)
    OUTPUT_PATH.write_text(html, encoding="utf-8")
    print(f"Wrote {OUTPUT_PATH} ({len(sessions)} session(s))")
    if open_in_browser:
        webbrowser.open(OUTPUT_PATH.as_uri())
    return OUTPUT_PATH


if __name__ == "__main__":
    generate_dashboard()
