"""find_plateaus tool: ALL exercises currently in a plateau (best_est_1rm
non-increasing across the full 4-week window), not just the one
find_progression_candidate might have picked as this week's plateau-
exception proposal. This is the secondary "flag other stalls" signal —
text-only observations, never a propose_progression call.

Reuses is_plateau from find_progression_candidate.py rather than
duplicating the definition — one plateau rule, not two that could drift
apart.
"""
from __future__ import annotations

from strands import tool

from tools.find_progression_candidate import entry_for, is_plateau
from tools.query_workout_history import fetch_history


def find_all(weeks: list[dict]) -> list[dict]:
    """Plain (undecorated) implementation, directly testable."""
    if not weeks:
        return []
    latest = weeks[-1]
    template_ids = sorted({
        exercise["exercise_template_id"]
        for exercise in latest.get("exercises", [])
        if exercise.get("exercise_template_id")
    })

    results = []
    for template_id in template_ids:
        if is_plateau(weeks, template_id):
            entry = entry_for(latest, template_id)
            first_value = entry_for(weeks[0], template_id)["best_est_1rm"]
            last_value = entry["best_est_1rm"]
            results.append({
                "exercise_template_id": template_id,
                "exercise_title": entry.get("exercise_title"),
                "best_est_1rm_drop": round(first_value - last_value, 2),
            })
    results.sort(key=lambda row: (-row["best_est_1rm_drop"], row["exercise_template_id"]))
    return results


@tool
def find_plateaus() -> dict:
    """
    Get every exercise currently stalled: best_est_1rm present and never
    increasing across all 4 weeks of history. This is broader than
    find_progression_candidate's plateau path (which only ever surfaces
    ONE plateaued exercise, and only as a last-resort progression
    candidate) — use this to flag OTHER stalls as plain-text
    observations in the report, separate from whatever
    find_progression_candidate chose. Never call propose_progression for
    anything returned here unless it's also the exact exercise
    find_progression_candidate picked.

    Returns:
        Dict with a "plateaus" list (each: exercise_template_id,
        exercise_title, best_est_1rm_drop), sorted biggest drop first.
        Empty list if nothing is currently plateaued.
    """
    history = fetch_history(weeks=4)
    return {"plateaus": find_all(history.get("weeks", []))}
