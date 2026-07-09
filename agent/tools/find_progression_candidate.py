"""find_progression_candidate tool: deterministic selection of AT MOST
ONE exercise eligible for a weight-progression proposal this week, from
the last 4 weeks of history. Pure computation, no LLM judgment — the
model was originally asked to do this arithmetic itself (count
qualifying weeks, track a weight streak across a JSON blob) and that's
exactly the kind of task LLMs get wrong: miscounts, skipped exercises,
non-reproducible results even at temperature=0. This tool replaces that
reasoning with code so the same 4-week data always produces the same
candidate.

Algorithm (see CLAUDE.md-adjacent design notes for the full rationale):
1. Only exercises present in the most recent week are considered.
2. racha_peso ("weight streak"): consecutive weeks, counting back from
   the most recent, where max_weight_kg is exactly unchanged. A missing
   week or a different weight breaks the streak. This encodes the real
   double-progression premise: reps only mean something as a readiness
   signal if the weight was actually held constant while they were
   logged — mean_reps averaged across weeks where the weight was also
   changing conflates two different signals.
3. Within that streak, qualifying_weeks = weeks where mean_reps >= 9.0.
4. REPS_CANDIDATOS = exercises with racha_peso >= 2 AND qualifying_weeks
   (within the streak) >= 2 — both the weight-held-constant condition and
   the reps-plateaued-at-that-weight condition, not just one.
5. PLATEAU_CANDIDATOS (fallback only if no REPS_CANDIDATOS): exercises
   present in all 4 weeks with best_est_1rm non-increasing week over week
   (a genuine stall, can still justify a proposal below the reps gate).
6. Selection: highest qualifying_weeks (reps path) or largest best_est_1rm
   drop (plateau path); ties broken by exercise_template_id ascending —
   arbitrary but fixed, so ties don't introduce nondeterminism either.

Read-only: only calls fetch_history, never writes anything.
"""
from __future__ import annotations

from typing import Optional

from strands import tool

from tools.query_workout_history import fetch_history

_REPS_THRESHOLD = 9.0
_MIN_STREAK = 2
_MIN_QUALIFYING_WEEKS = 2


def entry_for(week: dict, exercise_template_id: str) -> Optional[dict]:
    for exercise in week.get("exercises", []):
        if exercise.get("exercise_template_id") == exercise_template_id:
            return exercise
    return None


def weight_streak(weeks: list[dict], exercise_template_id: str) -> tuple[int, int]:
    """(racha_peso, qualifying_weeks_within_streak) for this exercise,
    counting back from the most recent week. weeks is oldest-to-newest,
    matching query_workout_history's own ordering."""
    entries_newest_first = [entry_for(w, exercise_template_id) for w in reversed(weeks)]

    if not entries_newest_first or entries_newest_first[0] is None:
        return 0, 0
    current_weight = entries_newest_first[0].get("max_weight_kg")
    if current_weight is None:
        return 0, 0

    streak = 0
    qualifying = 0
    for entry in entries_newest_first:
        if entry is None or entry.get("max_weight_kg") != current_weight:
            break
        streak += 1
        mean_reps = entry.get("mean_reps")
        if mean_reps is not None and mean_reps >= _REPS_THRESHOLD:
            qualifying += 1
    return streak, qualifying


def is_plateau(weeks: list[dict], exercise_template_id: str) -> bool:
    """True if the exercise appears in every week given with a non-null
    best_est_1rm, and it never increases week over week (a real stall,
    not just a data gap read as a stall)."""
    values = []
    for week in weeks:
        entry = entry_for(week, exercise_template_id)
        if entry is None or entry.get("best_est_1rm") is None:
            return False
        values.append(entry["best_est_1rm"])
    if len(values) < 2:
        return False
    return all(values[i] <= values[i - 1] for i in range(1, len(values)))


def find_candidate(weeks: list[dict]) -> Optional[dict]:
    """Plain (undecorated) implementation — the actual selection logic,
    directly testable without going through the @tool/DynamoDB layer."""
    if not weeks:
        return None
    latest = weeks[-1]
    template_ids = sorted({
        exercise["exercise_template_id"]
        for exercise in latest.get("exercises", [])
        if exercise.get("exercise_template_id")
    })

    reps_candidates = []
    for template_id in template_ids:
        streak, qualifying = weight_streak(weeks, template_id)
        if streak >= _MIN_STREAK and qualifying >= _MIN_QUALIFYING_WEEKS:
            latest_entry = entry_for(latest, template_id)
            latest_mean_reps = (latest_entry or {}).get("mean_reps") or 0
            reps_candidates.append((qualifying, latest_mean_reps, template_id))

    if reps_candidates:
        reps_candidates.sort(key=lambda row: (-row[0], -row[1], row[2]))
        return {"exercise_template_id": reps_candidates[0][2], "reason": "reps"}

    plateau_candidates = []
    for template_id in template_ids:
        if is_plateau(weeks, template_id):
            values = [entry_for(w, template_id)["best_est_1rm"] for w in weeks]
            drop = values[0] - values[-1]
            plateau_candidates.append((drop, template_id))

    if plateau_candidates:
        plateau_candidates.sort(key=lambda row: (-row[0], row[1]))
        return {"exercise_template_id": plateau_candidates[0][1], "reason": "plateau"}

    return None


@tool
def find_progression_candidate() -> dict:
    """
    Deterministically select at most one exercise eligible for a weight-
    progression proposal this week, from the last 4 weeks of history.
    Call this BEFORE propose_progression — it replaces any manual
    threshold/streak counting you'd otherwise have to do over raw
    query_workout_history data, which is unreliable to do by hand.

    Returns:
        If a candidate was found: exercise_template_id, exercise_title,
        and reason ("reps" — sustained reps at a held weight, or
        "plateau" — best_est_1rm stalled/dropping across all 4 weeks).
        If no exercise currently qualifies: {"candidate": None}. In that
        case, do not call propose_progression this run.
    """
    history = fetch_history(weeks=4)
    weeks = history.get("weeks", [])
    result = find_candidate(weeks)
    if result is None:
        return {"candidate": None}

    entry = entry_for(weeks[-1], result["exercise_template_id"])
    return {
        "exercise_template_id": result["exercise_template_id"],
        "exercise_title": (entry or {}).get("exercise_title"),
        "reason": result["reason"],
    }
