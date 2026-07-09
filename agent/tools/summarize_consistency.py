"""summarize_consistency tool: deterministic trend classification for
workout_count/total_sets/total_volume_kg across the 4-week window. Same
principle as find_progression_candidate — comparing a handful of numbers
across weeks and labeling "dropping/steady/rising" is exactly the kind
of judgment call that should be computed once in code, not re-derived by
the model from raw numbers each time (consistency, reproducibility).

Read-only: only calls fetch_history, never writes anything.
"""
from __future__ import annotations

from statistics import mean

from strands import tool

from tools.query_workout_history import fetch_history

_DROP_RATIO = 0.7
_RISE_RATIO = 1.3


def classify_trend(values: list[float]) -> str:
    """oldest-to-newest values -> "dropping"/"steady"/"rising"/
    "insufficient_data" (fewer than 2 weeks, or nothing logged before
    the latest week to compare against)."""
    if len(values) < 2:
        return "insufficient_data"
    prior = values[:-1]
    latest = values[-1]
    prior_avg = mean(prior)
    if prior_avg == 0:
        return "insufficient_data"
    ratio = latest / prior_avg
    if ratio < _DROP_RATIO:
        return "dropping"
    if ratio > _RISE_RATIO:
        return "rising"
    return "steady"


def summarize(weeks: list[dict]) -> dict:
    """Plain (undecorated) implementation, directly testable."""
    series = {
        "week": [w.get("week") for w in weeks],
        "workout_count": [w.get("workout_count") for w in weeks],
        "total_sets": [w.get("total_sets") for w in weeks],
        "total_volume_kg": [w.get("total_volume_kg") for w in weeks],
    }
    return {
        **series,
        "workout_count_trend": classify_trend(series["workout_count"]),
        "total_sets_trend": classify_trend(series["total_sets"]),
        "total_volume_kg_trend": classify_trend(series["total_volume_kg"]),
    }


@tool
def summarize_consistency() -> dict:
    """
    Get the 4-week trend classification for workout_count, total_sets,
    and total_volume_kg — already labeled "dropping"/"steady"/"rising"/
    "insufficient_data" per metric, so you don't need to eyeball the raw
    numbers yourself. "dropping" on workout_count/total_sets signals
    fading consistency/adherence; "rising" total_volume_kg alongside a
    flat/dropping progression call can indicate fatigue accumulation
    (cross-check against find_progression_candidate's result).

    Returns:
        Dict with per-week series (week, workout_count, total_sets,
        total_volume_kg) plus workout_count_trend/total_sets_trend/
        total_volume_kg_trend labels.
    """
    history = fetch_history(weeks=4)
    return summarize(history.get("weeks", []))
