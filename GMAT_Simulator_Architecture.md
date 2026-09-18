# GMAT Focus Edition Simulator — V3 Architecture & Build Guide

*Built by a fellow aspirant who finished his journey. Shared so you can build your own.*

This document is the complete architecture and engine specification for a locally-hosted GMAT Focus Edition CAT (Computer Adaptive Test) simulator. It is designed so that you can feed it directly to an AI coding assistant — Antigravity, Claude Code, Cursor, Windsurf, or any other — and have it generate the full working codebase on your machine. No untrusted `.py` files, no random downloads. You build it yourself, you own it, and you can customize it to match your own understanding of the GMAT.

> **Important Caveat on Scoring:** The scores produced by this simulator are approximations for practice purposes. They will not perfectly match official GMAT scores because GMAC's exact scoring algorithm is proprietary and undisclosed. The engine described here is calibrated against publicly available data (GMAT Club community statistics, ESR reports, and official score percentile tables), but it is an educated model, not a replica. Treat it as a diagnostic and training tool. The architecture is fully open to customization — if your own research or ESR data suggests different penalty weights, delta values, or mapping curves, adjust them.

---

## Table of Contents

1. [Data Sourcing](#1-data-sourcing)
2. [Question Bank Structure](#2-question-bank-structure)
3. [Section Blueprints & Category Quotas](#3-section-blueprints--category-quotas)
4. [Core Engine — The Dual-Track System](#4-core-engine--the-dual-track-system)
5. [IRT Parameters & Band Mapping](#5-irt-parameters--band-mapping)
6. [Track 1: Percentile System (Scoring)](#6-track-1-percentile-system-scoring)
7. [Track 2: IRT Theta System (Question Selection)](#7-track-2-irt-theta-system-question-selection)
8. [Question Selection Algorithm](#8-question-selection-algorithm)
9. [Early Profile Anchor & Penalty Logic](#9-early-profile-anchor--penalty-logic)
10. [Streak-Based Band Jump Logic](#10-streak-based-band-jump-logic)
11. [Multi-Source Reasoning (MSR) Dynamics](#11-multi-source-reasoning-msr-dynamics)
12. [Final Score Computation](#12-final-score-computation)
13. [Browser Automation & UI](#13-browser-automation--ui)
14. [Timer System](#14-timer-system)
15. [Review Phase](#15-review-phase)
16. [Session Management](#16-session-management)
17. [Analytics Dashboard](#17-analytics-dashboard)
18. [Project Structure](#18-project-structure)
19. [How to Prompt Your AI to Build This](#19-how-to-prompt-your-ai-to-build-this)

---

## 1. Data Sourcing

The question bank is built from **GMAT Club** forum data. 

### 1.1 Scraping with Instant Data Scraper

1. Install the **Instant Data Scraper** Chrome extension.
2. Navigate to GMAT Club's question directories for each section:
   - Quantitative Reasoning (PS questions)
   - Data Insights (DS, Graphics Interpretation, Table Analysis, Two-Part Analysis, MSR)
   - Verbal Reasoning (Critical Reasoning, Reading Comprehension)
3. Activate the scraper on each forum page. It will detect the table of question threads automatically.
4. Paginate through all pages, letting the scraper accumulate rows.
5. Export as CSV (e.g., `gmatclub.csv`). The key columns you need are:
   - **URL** (the forum thread link for each question)
   - **Tags** (contains difficulty labels like "655-705", topic tags like "Algebra", and format tags like "Data Sufficiency")

### 1.2 What the Raw CSV Looks Like

The CSV from Instant Data Scraper will have messy column names (often containing `href`, `topic-link`, `tags`). Your bank builder script needs to:
- Auto-detect the URL column (look for columns containing `topic-link` or `href`).
- Auto-detect the tag columns (look for columns containing `tags`).
- Filter out non-question rows (overview pages, directory links, etc.).
- Parse difficulty labels from the tag text using regex patterns like `sub 505`, `505-555`, `555-605`, `605-655`, `655-705`, `705-805`, `805+`.

---

## 2. Question Bank Structure

Your bank builder should output a JSON file where each question is a dictionary with these fields:

```json
{
  "id": "q-a1b2c3d4e5",
  "url": "https://gmatclub.com/forum/if-x-is-a-positive-integer-12345.html",
  "difficulty_band": "Hard",
  "difficulty_index": 5,
  "difficulty_anchor": 0.15,
  "category": "Equal/Unequal/ALG",
  "subtopic": "Inequalities (Unequal)",
  "delivery_type": "single",
  "active": true
}
```

| Field | Description |
|-------|-------------|
| `id` | Stable hash of the URL (`sha1(url)[:10]`, prefixed with `q-`) |
| `url` | The GMAT Club forum thread URL |
| `difficulty_band` | Human-readable band label (see §5) |
| `difficulty_index` | Integer 1-8 (see §5) |
| `difficulty_anchor` | The IRT b-parameter for that band (see §5) |
| `category` | The section-specific category (see §3) |
| `subtopic` | Finer-grained topic within the category |
| `delivery_type` | `"single"` for standalone questions, `"MSR"` for Multi-Source Reasoning prompts |
| `active` | Boolean, set to `false` after the question is used |

### Difficulty Label → Band Mapping

The GMAT Club difficulty tags map directly to band indices:

| Raw GMAT Club Tag | Band Label | Band Index |
|-------------------|------------|------------|
| `sub 505` | Very Easy | 1 |
| `505-555` | Easy | 2 |
| `555-605` | Easy-Medium | 3 |
| `605-655` | Medium | 4 |
| `655-705` | Hard | 5 |
| `705-805` | Very Hard | 6 |
| `805+` | Extreme | 7 |
| *(reserved)* | Abnormally Hard | 8 |

Band 8 is reserved for outlier questions that are harder than the standard `805+` classification. You can manually assign these or leave Band 8 empty.

### Category & Subtopic Mapping (Quant Example)

Use keyword heuristics on the tag text to classify questions. Here is the Quant mapping:

| Tag Keywords | Category | Subtopic |
|-------------|----------|----------|
| probability, permutations, combinations | Counting/Sets/Series/Prob/Stats | Counting & Probability |
| overlapping sets, venn | Counting/Sets/Series/Prob/Stats | Overlapping Sets |
| sequence, series, progression | Counting/Sets/Series/Prob/Stats | Sequences & Series |
| statistics, mean, median, standard deviation | Counting/Sets/Series/Prob/Stats | Basic Statistics |
| distance, speed, rate, work, mixture | Rates/Ratio/Percent | Rates & Work |
| ratio, proportion, fractions | Rates/Ratio/Percent | Ratios & Proportions |
| percent, interest, profit, discount | Rates/Ratio/Percent | Percentages |
| inequality | Equal/Unequal/ALG | Inequalities |
| equation, linear, quadratic | Equal/Unequal/ALG | Equalities |
| algebra, absolute value, coordinate geometry | Equal/Unequal/ALG | Algebraic Manipulation |
| factor, multiple, lcm, gcd, prime | Value/Order/Factors | Factors & Multiples |
| number properties, remainder, exponent, root | Value/Order/Factors | Value & Number Properties |

---

## 3. Section Blueprints & Category Quotas

The GMAT Focus Edition has three sections. Each has a fixed number of questions and a specific category distribution.

### Quantitative Reasoning — 21 questions, 45 minutes

| Category | Quota |
|----------|-------|
| Counting/Sets/Series/Prob/Stats | 5 |
| Rates/Ratio/Percent | 5 |
| Equal/Unequal/ALG | 6 |
| Value/Order/Factors | 5 |

### Data Insights — 20 questions, 45 minutes

DI uses randomized blueprints with weighted probability:

**Standard Blueprint (75% chance):**

| Category | Quota |
|----------|-------|
| Data Sufficiency | 7 |
| Graphs and Tables | 6 |
| MSRs | 3 |
| Two-Part Analysis | 4 |

*Sub-quotas:* Graphics Interpretation: 3, Table Analysis: 3

**Extended MSR Blueprint (25% chance):**

| Category | Quota |
|----------|-------|
| Data Sufficiency | 6 |
| Graphs and Tables | 5 |
| MSRs | 6 |
| Two-Part Analysis | 3 |

*Sub-quotas:* Graphics Interpretation: 3, Table Analysis: 2

### Verbal Reasoning — 23 questions, 45 minutes

| Category | Quota |
|----------|-------|
| Critical Reasoning | 10 |
| Reading Comprehension | 13 |

---

## 4. Core Engine — The Dual-Track System

This is the most important architectural concept. The V3 engine runs **two parallel systems simultaneously**:

| System | Purpose | What It Tracks |
|--------|---------|----------------|
| **Track 1: Percentile System** | Drives the **final score** | Category percentiles, overall percentile, early anchor |
| **Track 2: IRT Theta System** | Drives **question selection** | Per-category theta (ability estimate) via MAP estimation |

After every response, both tracks are updated. They influence different parts of the engine:
- **Theta** decides *which question to serve next* (via Fisher Information ranking within IRT windows).
- **Percentile** decides *what your final scaled score will be* (via delta tables and piecewise mapping).

This hybrid approach gives mathematically optimal question targeting (IRT) while preserving the band-based scoring structure that best approximates the real GMAT's reported percentile behavior.

### State Model

The engine maintains these data structures:

```python
@dataclass
class CategoryState:
    name: str
    attempts: int = 0
    correct: int = 0
    percentile: float = 60.0          # Track 1: discrete percentile
    band_index: float = 4.0           # smoothed band index
    streak: int = 0                   # consecutive correct in this category
    last_seen_at: int = -1            # question index (1-based), -1 = never
    confidence: float = 0.0           # 0-1, used for category prioritization
    max_band_index_seen: int = 0
    theta: float = 0.0               # Track 2: IRT ability estimate
    response_history: List[Tuple[float, float, float, bool]] = []  # (b, a, c, was_correct)

@dataclass
class SectionState:
    section_name: str
    categories: Dict[str, CategoryState]
    overall_percentile: float = 60.0
    question_index: int = 0
    total_questions: int = 21
    category_quotas: Dict[str, int]
    answered_by_category: Dict[str, int]
    exhausted_categories: set
    
    # Early anchor
    early_anchor_percentile: float = 60.0
    early_anchor_locked: bool = False
    
    # Streaks
    global_correct_streak: int = 0
    category_correct_streak: Dict[str, int]
    early_jump_level: int = 0
    
    # Early miss tracking
    early_easy_medium_miss: bool = False
    early_easy_wrong_count: int = 0
    section_percentile_for_scale: float = 60.0
    scaled_score: int = 60
    
    # Seed
    seed_overall_percentile: float = 60.0
```

---

## 5. IRT Parameters & Band Mapping

Every question has three IRT parameters derived from its difficulty band. These are the empirically calibrated tables used by the V3 engine.

| Band Index | Band Label | Baseline Pct | IRT b (Difficulty) | IRT a (Discrimination) |
|------------|------------|--------------|---------------------|------------------------|
| 1 | Very Easy | 20% | -1.60 | 0.6 |
| 2 | Easy | 33% | -0.90 | 0.8 |
| 3 | Easy-Medium | 50% | -0.60 | 0.8 |
| 4 | Medium | 70% | -0.25 | 1.0 |
| 5 | Hard | 85% | +0.15 | 1.1 |
| 6 | Very Hard | 95% | +0.42 | 1.2 |
| 7 | Extreme | 99% | +1.00 | 1.3 |
| 8 | Abnormally Hard | 99.5% | +1.50 | 1.3 |

**Pseudo-guessing parameter (c):** `0.20` universally for all 5-option questions.

**Design note:** These b-values are *empirically calibrated*, not probit-derived. A probit conversion of the band percentages would give different values (e.g., Band 7 at 99% would be ≈2.33 via probit). The engine deliberately uses lower b-values to create a higher practice ceiling without compressing the difficulty range.

---

## 6. Track 1: Percentile System (Scoring)

After every response, the engine updates category and overall percentiles using **delta tables** — fixed point adjustments based on the band of the question answered.

### 6.1 Delta Tables for Single-Part Questions

```python
DELTA_CORRECT = {1: +4, 2: +5, 3: +6, 4: +7, 5: +8, 6: +9, 7: +10, 8: +11}
DELTA_WRONG   = {1: -10, 2: -9, 3: -8, 4: -7, 5: -5, 6: -3, 7: -1, 8: -0.5}
```

Getting a Band 1 (Very Easy) question wrong costs you -10 percentile points. Getting a Band 8 (Abnormally Hard) question wrong only costs -0.5. This asymmetry is intentional — the GMAT punishes easy misses far more than hard misses.

### 6.2 Delta Tables for MSR Child Questions

MSR sub-questions use slightly reduced deltas:

```python
CHILD_DELTA_CORRECT = {1: +3, 2: +4, 3: +5, 4: +6, 5: +7, 6: +8, 7: +9, 8: +10}
CHILD_DELTA_WRONG   = {1: -10, 2: -9, 3: -8, 4: -7, 5: -5, 6: -3, 7: -1, 8: -0.5}
```

### 6.3 Overall Percentile Update Logic

The overall percentile does not move by the same amount as the category percentile. It uses a dampened version:

- **Base:** `delta_overall = 0.4 × delta_category`
- **Correct answer caps** (prevents easy questions from inflating the score):
  - Band ≤4: max +1.5 to overall
  - Band 5: max +2.5
  - Band 6: max +3.6
  - Band 7: max +4.0
  - Band 8: max +4.5
- **Wrong answer in early phase (Q1-Q6):** Uses the severe `EARLY_WRONG_PENALTY` table (see §9)
- **Wrong answer in later phase (Q7+):** Uses the softer `LATER_WRONG_PENALTY` table (see §9)

### 6.4 Band Index Smoothing

After each response, the category's band index is smoothed:
```python
cat.band_index = 0.8 * cat.band_index + 0.2 * percentile_to_band_index(cat.percentile)
```

### 6.5 Percentile-to-Band Conversion

```python
def percentile_to_band_index(p):
    if p < 25:  return 1
    if p < 40:  return 2
    if p < 60:  return 3
    if p < 80:  return 4
    if p < 93:  return 5
    if p < 98:  return 6
    if p < 99.5: return 7
    return 8
```

---

## 7. Track 2: IRT Theta System (Question Selection)

### 7.1 The 3PL Model

The probability of a correct response for a test-taker with ability θ on item i:

$$P_i(\theta) = c + \frac{1 - c}{1 + e^{-1.7 \cdot a_i \cdot (\theta - b_i)}}$$

### 7.2 MAP Theta Estimation

After every response, the engine re-estimates θ for the category using all responses in that category's history. It maximizes the log-posterior:

$$\ln L(\theta) = \sum_{i=1}^{n} \left[ u_i \ln P_i(\theta) + (1 - u_i) \ln(1 - P_i(\theta)) \right] - \frac{\theta^2}{2\sigma^2}$$

The final term is the log of the normal prior N(0, σ²).

**Prior schedule:**
- σ = 1.5 for ≤10 responses (tighter prior, more conservative)
- σ = 2.0 for >10 responses (wider, lets the data speak)

**Implementation:** Use `scipy.optimize.minimize_scalar` with bounds `[-3.5, 3.5]`, method `'bounded'`. Minimize the negation of the log-posterior. Include a grid-search fallback (step 0.1 from -3.5 to +3.5) for environments without scipy.

### 7.3 Fisher Information for Item Ranking

```python
I_i(θ) = a² × ((P - c)² / (1 - c)²) × ((1 - P) / P)
```

**Note:** The standard 3PL Fisher Information formula includes a 1.7² factor. The V3 engine deliberately omits this constant because it does not affect the relative ranking of candidates and avoids unnecessary scaling.

A small difficulty bonus is added to the composite score to encourage ceiling exploration:

```python
composite_score = I + 0.05 × max(0, b - θ)
```

---

## 8. Question Selection Algorithm

### 8.1 Category Selection (Priority Formula)

Before selecting a question, the engine first chooses which *category* to pull from. The priority formula balances three factors:

```python
priority = 0.5 × quota_gap_norm + 0.3 × low_confidence + 0.2 × age_norm
```

Where:
- `quota_gap_norm` = how far behind this category is on its quota
- `low_confidence` = 1 - confidence (categories with fewer attempts get priority)
- `age_norm` = how many questions since this category was last seen

Ties are broken randomly.

### 8.2 IRT Windowing

Once the category is chosen, the engine filters candidates from the question bank:

1. **Tight window:** candidates where `b ∈ [θ_cat - 0.5, θ_cat + 0.5]`
2. **Wider fallback:** if fewer than 3 candidates, expand to `[θ_cat - 1.0, θ_cat + 1.0]`
3. **Full fallback:** if still fewer than 3, use all candidates in the category

### 8.3 Ceiling Probe Mode

If the user gets 3 consecutive correct answers in a category, and all 3 had `b ≥ θ_cat - 0.2` (i.e., they weren't too easy), the engine enters **ceiling probe mode**:

- Probe window: `b ≥ max(θ_cat + 0.5, 0.42)` up to `b ≤ 1.55`
- This forces the engine to serve much harder items to test whether the user's ability ceiling is higher than estimated
- If no candidates exist in the probe window, falls back to the hardest available items

### 8.4 Final Selection

Within the filtered window:
1. Rank all candidates by `composite_score = Fisher_Information + 0.05 × max(0, b - θ)`
2. Take the top 5
3. Randomly select one from those 5 (prevents predictability, simulates real GMAT item exposure control)

---

## 9. Early Profile Anchor & Penalty Logic

The GMAT Focus heavily weights the first few questions. The V3 engine replicates this with explicit constants.

### 9.1 Early Anchor Lock

- **Early profile window:** Questions 1-6 (configurable via `EARLY_PROFILE_QUESTIONS = 6`)
- At Q6, the current `overall_percentile` is locked as `early_anchor_percentile`
- After Q6, the effective percentile used for question selection becomes a weighted blend:

```python
effective = 0.80 × early_anchor_percentile + 0.20 × live_overall_percentile
```

This prevents wild swings in the second half of the test from dramatically changing the difficulty of served questions.

### 9.2 Early Wrong Penalties (Q1-Q6)

If you get an Easy or Medium question wrong in Q1-Q6, the engine applies severe penalties:

```python
EARLY_WRONG_PENALTY = {
    1: -20.0,   # Very Easy
    2: -18.0,   # Easy
    3: -15.0,   # Easy-Medium
    4: -12.0,   # Medium
    5: -5.0,    # Hard
    6: -3.0,    # Very Hard
    7: -1.5,    # Extreme
    8: -1.0,    # Abnormally Hard
}
```

Missing an Easy/Medium question (Band ≤ 4) in the early phase also sets `early_easy_medium_miss = True`, which:
- Caps the maximum achievable scaled score at **84** (out of 90)
- Caps `section_percentile_for_scale` at `early_anchor_percentile + 12.0`, with a floor of 70.0
- Blocks streak-based band jumps

### 9.3 Later Wrong Penalties (Q7+)

```python
LATER_WRONG_PENALTY = {
    1: -8.0,
    2: -7.0,
    3: -6.0,
    4: -5.0,
    5: -3.5,
    6: -2.0,
    7: -1.0,
    8: -0.5,
}
```

---

## 10. Streak-Based Band Jump Logic

When a user answers multiple questions correctly in a row, the engine jumps the target difficulty band upward to test the ceiling faster.

### 10.1 Jump Calculation

```
Global streak ≤ 1  →  global_jump = 0
Global streak 2-3  →  global_jump = 1
Global streak ≥ 4  →  global_jump = 2

Category streak ≥ 2  →  category_limit = 2
Category streak < 2  →  category_limit = 1

effective_jump = min(global_jump, category_limit)
```

### 10.2 Safety Rails

- **Early phase (Q1-Q6):** Jump is capped so target never exceeds Band 6. If `early_easy_medium_miss` is set, jumps are blocked entirely.
- **Band 8 access (later phase):** Requires ALL of: global streak ≥ 4, category streak ≥ 2, effective percentile ≥ 85, and no early easy miss.

---

## 11. Multi-Source Reasoning (MSR) Dynamics

MSR questions contain multiple sub-questions (typically 3) about a shared prompt. They are scored differently:

### 11.1 Percentile Update

Each child's individual contribution is computed using `CHILD_DELTA_CORRECT/WRONG` tables against the `BAND_BASELINE`. The final MSR delta is the average of all children's percentiles, with a bonus of +2 if all children are correct AND all were Band 5+.

The category percentile update is dampened: `cat.percentile += 0.7 × effective_delta`

### 11.2 Theta Update

Each MSR child is treated as an **independent IRT observation**. All children's (b, a, c, correct) tuples are appended to the category's `response_history`, then theta is re-estimated via MAP.

### 11.3 Early Phase MSR Band Caps

- Overall percentile < 70: MSR target capped at Band 4
- Overall ≥ 70 + global streak ≥ 3 + no early miss: capped at Band 5
- Overall ≥ 80 + global streak ≥ 4 + no early miss: capped at Band 6
- Band 8 MSRs in later phase require: global streak ≥ 4, effective percentile ≥ 85, no early miss

---

## 12. Final Score Computation

### 12.1 Category Finalization

Each category's final percentile is derived from its theta via the normal CDF:

```python
cat_percentile_final = norm_cdf(category_theta) × 100
```

Where `norm_cdf(x) = (1 + erf(x / √2)) / 2`

### 12.2 Section Percentile

```python
raw_section_percentile = 0.75 × avg_category_percentile + 0.25 × overall_percentile
```

If `early_easy_medium_miss` is set:
```python
early_ceiling = early_anchor_percentile + 12.0
section_percentile_for_scale = min(raw_section_percentile, max(early_ceiling, 70.0))
```

### 12.3 Scaled Score (Percentile → 60-90)

The scaled score uses **piecewise interpolation tables**, not a linear formula. These tables map percentiles to scaled scores and differ by section:

**Quant Mapping:**
```python
QUANT_MAPPING = [
    (1, 61), (2, 63), (3, 64), (4, 65), (5, 66), (7, 67), (9, 68), (11, 69),
    (14, 70), (17, 71), (20, 72), (23, 73), (28, 74), (33, 75), (38, 76),
    (43, 77), (50, 78), (57, 79), (64, 80), (70, 81), (75, 82), (80, 83),
    (85, 84), (88, 85), (91, 86), (93, 87), (95, 88), (97, 89), (100, 90)
]
```

**DI Mapping:**
```python
DI_MAPPING = [
    (3, 60), (4, 61), (5, 63), (7, 64), (8, 65), (10, 66), (11, 67), (14, 68),
    (17, 69), (20, 70), (24, 71), (29, 72), (34, 73), (40, 74), (46, 75),
    (52, 76), (61, 77), (69, 78), (76, 79), (83, 80), (88, 81), (93, 82),
    (95, 83), (97, 84), (98, 85), (99, 88), (100, 90)
]
```

Between two data points, the score is linearly interpolated. For the Verbal section, you can use the Quant mapping as a starting point and adjust based on your own ESR data.

If `early_easy_medium_miss` is set, the final scaled score is hard-capped at **84**.

---

## 13. Browser Automation & UI

### 13.1 Technology

The simulator uses **`undetected_chromedriver`** (not regular Selenium) to bypass Cloudflare protection on GMAT Club. Install it via `pip install undetected-chromedriver`.

A **persistent Chrome profile** (`.gmatclub_uc_profile/` directory) stores login cookies so you only need to log into GMAT Club once. On subsequent runs, the browser reuses the session.

### 13.2 CSS Feedback Masking

Inject CSS to hide all feedback elements before the user sees the page:

```css
/* Hide correctness feedback, solutions, explanations, difficulty tags */
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

/* Hide percentage bars that replace A-E options after submission */
.statisticWrap:has(.answerPercentage),
.statisticWrapExisting:has(.answerPercentage) {
    opacity: 0 !important;
    pointer-events: none !important;
    position: absolute !important;
    left: -9999px !important;
}
```

Additionally, inject JavaScript to match difficulty labels by text content (`sub 505`, `505-555`, etc.) and hide them, since some are not caught by CSS class selectors alone.

**Critical design note:** Feedback is hidden, not blocked. The DOM elements remain present so the submission interceptor can read correctness data from them.

### 13.3 Submission Interceptor

The engine detects the user's answer and its correctness via:
1. **Monkey-patching `timer_answer()`:** GMAT Club's built-in JS function is wrapped to capture the user's choice (11=A, 12=B, 13=C, 14=D, 15=E) and immediately hide the options container.
2. **MutationObserver:** Watches the answer container (`#timer_abcde`) for DOM mutations that reveal `.correctAnswer` and `.selectedAnswer` CSS classes, then stores the result in `window.__gmat_result`.

### 13.4 HUD Overlay

Inject a fixed-position HTML/CSS/JS overlay in the top-right corner:

```
┌─────────────────────────────────┐
│  Q 7 / 21    ⏱ 32:15 remaining │
│  [☆ Save for Review]           │
└─────────────────────────────────┘
```

- **Styling:** `position: fixed; top: 12px; right: 16px; z-index: 99999; background: rgba(30,30,30,0.92); color: white; border-radius: 8px; font-family: 'Segoe UI', sans-serif`
- **Timer:** A JS `setInterval` countdown seeded with the remaining seconds from Python. Turns red at ≤5 minutes.
- **Bookmark button:** Toggles ☆/★ and stores question IDs in `window.__gmat_review_list`.
- **Re-injection:** The HUD must be re-injected on every page navigation (each question is a new URL). The timer is re-seeded from the Python accumulator each time.

### 13.5 DOM Data Scraping

After masking is injected, scrape statistics from the GMAT Club DOM for analytics:
- Difficulty percentage
- Correct/Wrong percentages
- Average time for correct/wrong answers
- Sample size
- Answer distribution (A/B/C/D/E percentages)

Store all scraped data in the session report for later analysis. This data is not used for scoring — it is purely diagnostic.

---

## 14. Timer System

### 14.1 Thinking-Time-Only Clock

The 45-minute budget counts **thinking time only**, excluding page loads:

```python
TIME_BUDGET_SECONDS = 2700
elapsed_thinking_seconds = 0  # accumulates after each question
```

After each question:
1. Read the per-question elapsed time from the GMAT Club DOM timer element.
2. Add it to `elapsed_thinking_seconds`.
3. Compute `remaining = 2700 - elapsed_thinking_seconds`.
4. If remaining ≤ 0: session status = `"EXPIRED"`.

### 14.2 Auto-Start Timer

On each page load, the script auto-clicks the GMAT Club timer play button via JS injection. This ensures timing begins only when the question is interactive.

### 14.3 Unanswered Penalty

If time expires before all questions are answered:

```python
theta_penalized = theta - 0.4 × N_unanswered
```

Clamped to [-3.5, 3.5]. Leaving 3 questions unanswered drops theta by 1.2, roughly a 6-point score hit.

---

## 15. Review Phase

After the last question (Q21 for Quant, Q20 for DI, Q23 for Verbal):

1. Display a terminal table showing all questions with their category, bookmark flag (★), and link.
2. User can enter any question number to navigate back to it.
3. The user has **3 answer change opportunities**. Only submitting a *different* answer counts as an edit.
4. After all edits are used or user types `done`, the engine re-runs MAP estimation on the updated response vector.

---

## 16. Session Management

### 16.1 Crash Recovery

After every question, the engine saves a full session snapshot to `active_session.json`. If the script crashes, re-running it detects the file and offers to resume.

### 16.2 Used Question Ledger

A JSON ledger tracks every question ever served, with:
- Question ID, URL (normalized), timestamp, session ID

On startup, the engine builds exclusion sets from the ledger and the current session to avoid repeats. URL normalization strips query parameters, fragments, and trailing slashes to catch duplicates.

### 16.3 Master Score Report

Every completed session appends a row to `score_report_master_v2.csv` with columns including: session ID, timestamp, section, engine mode, scaled score, accuracy, time used, theta trajectory, accuracy by band, accuracy by category, and diagnostic data from all engine tracks.

---

## 17. Analytics Dashboard

An `analytics_dashboard.py` script generates an HTML report (`analytics_report.html`) with:
- Session selector to browse historical tests
- Summary stats (score, accuracy, time)
- Theta/percentile trajectory charts
- Subcategory performance bars
- Sortable question-level detail table

The launcher menu offers a one-click option to generate and open this dashboard.

---

## 18. Project Structure

```
GMAT_Simulator/
├── launcher.py                     # Menu: start any section, open reports, generate dashboard
├── GMAT_Simulator.exe              # Compiled launcher for Windows
├── score_report_master_v2.csv      # Append-only master CSV
├── analytics_dashboard.py          # HTML dashboard generator
├── analytics_report.html           # Generated dashboard
│
├── engine/                         # Shared modules
│   ├── banded_scorer_v3.py         # V3 hybrid engine (all band + IRT logic)
│   ├── adaptive_scorer.py          # Pure IRT math (3PL, MAP, Fisher Info, scoring)
│   ├── dual_scoring.py             # Runs IRT + Banded engines in parallel
│   ├── dom_scraper.py              # JS to scrape question stats & read timer from DOM
│   ├── hud_overlay.py              # JS to render HUD overlay
│   ├── exclusion.py                # URL normalization, deduplication, used-question tracking
│   ├── score_report.py             # CSV master report append logic
│   ├── report_normalizer.py        # Normalizes JSON report schemas
│   └── progress_display.py         # Terminal progress formatting
│
├── Quant/
│   ├── run_quant_section_v3.py     # Main runner (21 Qs, 45 min)
│   ├── build_quant_bank.py         # Parses gmatclub.csv → questions_quant_v1.json
│   ├── gmatclub.csv                # Raw scraped data
│   ├── questions_quant_v1.json     # Question bank
│   ├── used_questions.json         # Ledger
│   └── quant_reports/              # Per-session JSON reports
│
├── DI_Adaptive/
│   ├── run_di_section.py           # Main runner (20 Qs, 45 min)
│   ├── questions_di_v1.json        # Question bank
│   └── di_reports/                 # Per-session JSON reports
│
├── Verbal/
│   ├── run_verbal_section.py       # Main runner (23 Qs, 45 min)
│   ├── questions_verbal_v1.json    # Question bank
│   └── verbal_reports/             # Per-session JSON reports
│
└── config/
    └── quant_band_cutoffs.json     # Band classification thresholds
```

---

## 19. How to Prompt Your AI to Build This

Feed this entire document to your AI assistant. Then use these prompts in sequence. Each prompt builds on the previous one.

---

**Prompt 1: Bank Builder**

> "Using the architecture document I've provided, write a `build_quant_bank.py` script. It should read `gmatclub.csv` (scraped from GMAT Club using Instant Data Scraper), auto-detect the URL and tag columns, parse difficulty labels (sub 505, 505-555, etc.) into the 1-8 Band Index system from section 5, map tags to the 4 Quant categories and subtopics from section 3, generate stable IDs via SHA1 hash, and output `questions_quant_v1.json` as described in section 2. Also output a `gmatclub_clean_quant.csv` with the cleaned data and print band/category distribution stats."

---

**Prompt 2: V3 Adaptive Engine**

> "Now write `engine/banded_scorer_v3.py` — the complete V3 hybrid engine. Implement ALL of: the dual-track system (section 4), the delta tables (section 6), the MAP theta estimation (section 7), the category selection priority formula (section 8.1), IRT windowing with ceiling probe mode (sections 8.2-8.4), the early anchor and penalty system (section 9), the streak-based band jump logic (section 10), MSR scoring (section 11), and the final score computation with piecewise percentile-to-scaled-score mapping (section 12). Include all constants and tables exactly as specified."

---

**Prompt 3: Supporting Engine Modules**

> "Write these supporting modules: (1) `engine/adaptive_scorer.py` — the pure IRT math functions: 3PL p_correct, log_posterior, MAP estimation, Fisher Information, standard error, theta_to_score linear mapping, and unanswered penalty. (2) `engine/dom_scraper.py` — JS injection functions to scrape question statistics and read the DOM timer. (3) `engine/hud_overlay.py` — JS to inject the HUD overlay with timer countdown, question counter, and bookmark button. (4) `engine/exclusion.py` — URL normalization, deduplication, and used-question ledger management. (5) `engine/score_report.py` — append-to-CSV logic for the master score report."

---

**Prompt 4: Section Runner**

> "Write `Quant/run_quant_section_v3.py` — the main test runner. It should: launch undetected_chromedriver with persistent profile, check for login, offer session resume from active_session.json, prompt for starting percentile and question count, run the main loop (select question → navigate → inject CSS mask → inject HUD → inject submission interceptor → auto-start timer → wait for answer → scrape DOM stats → read DOM timer → update both engine tracks → save session state), then run the review phase, compute final scores, and append to the master CSV. Use the architecture document for all constants and mechanics."

---

**Prompt 5: Analytics Dashboard**

> "Write `analytics_dashboard.py` that reads all JSON session reports from quant_reports/, di_reports/, and verbal_reports/, normalizes them into a common schema, and generates a single `analytics_report.html` with: a session selector dropdown, summary statistics, a theta/percentile trajectory chart, subcategory performance bars, and a sortable question-level detail table. Use inline HTML/CSS/JS (no external dependencies)."

---

**Prompt 6: Launcher**

> "Write `launcher.py` — a terminal menu that offers: [1] Start Quant Section, [2] Start Verbal Section, [3] Start Data Insights Section, [4] Open Master Score Report CSV, [5] Generate & Open Analytics Dashboard, [6] Exit. Each section option should let the user choose the engine mode (IRT v1, Banded v2, or Banded+IRT Hybrid v3). Wire up subprocess calls to the appropriate runner scripts."

---

### Dependencies

```
pip install undetected-chromedriver selenium scipy numpy
```

You will also need Google Chrome installed.

---

### Final Notes

This architecture represents months of iterative development, testing against real GMAT scores, and calibration against ESR (Enhanced Score Reports). The constants, tables, and formulas are not arbitrary — they were tuned through many practice sessions and compared against official results. That said, the GMAT's actual algorithm is proprietary, so treat everything here as an informed approximation that is open to your own refinement. 

If you discover better calibration values from your own ESR data, adjust the delta tables, penalty weights, and mapping curves accordingly. The architecture is designed to be modular — every constant is defined at the top of its file, never buried inside functions.

Good luck with your prep.
