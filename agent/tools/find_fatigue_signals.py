"""find_fatigue_signals tool: exercises where per-exercise
total_volume_kg is rising while max_weight_kg is flat or dropping across
the 4-week window — volume accumulating without load adaptation, a
common blind spot. Deterministic, same reasoning as the other analysis
tools: comparing a handful of numbers across weeks belongs in code, not
in the model's head.

Read-only: only calls fetch_history, never writes anything.
"""
from __future__ import annotations

from strands import tool

from tools.find_progression_candidate import entry_for
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
        present_entries = [entry_for(w, template_id) for w in weeks]
        present_entries = [e for e in present_entries if e is not None]
        if len(present_entries) < 2:
            continue

        volumes = [e.get("total_volume_kg") for e in present_entries]
        weights = [e.get("max_weight_kg") for e in present_entries]
        if any(v is None for v in volumes) or any(w is None for w in weights):
            continue

        volume_rising = volumes[-1] > volumes[0]
        weight_flat_or_dropping = weights[-1] <= weights[0]
        if volume_rising and weight_flat_or_dropping:
            results.append({
                "exercise_template_id": template_id,
                "exercise_title": present_entries[-1].get("exercise_title"),
                "total_volume_kg_change": round(volumes[-1] - volumes[0], 2),
                "max_weight_kg_change": round(weights[-1] - weights[0], 2),
            })

    results.sort(key=lambda row: (-row["total_volume_kg_change"], row["exercise_template_id"]))
    return results


@tool
def find_fatigue_signals() -> dict:
    """
    Get every exercise where training volume is climbing but the weight
    lifted isn't — total_volume_kg higher now than at the start of the
    4-week window, while max_weight_kg is flat or lower. This is
    accumulating fatigue without real adaptation, worth flagging even
    though it's not a progression proposal. Exercises with fewer than 2
    logged weeks in the window, or missing volume/weight data, are
    skipped (not enough signal either way).

    Returns:
        Dict with a "fatigue_signals" list (each: exercise_template_id,
        exercise_title, total_volume_kg_change, max_weight_kg_change),
        sorted by largest volume increase first. Empty list if nothing
        matches.
    """
    history = fetch_history(weeks=4)
    return {"fatigue_signals": find_all(history.get("weeks", []))}
