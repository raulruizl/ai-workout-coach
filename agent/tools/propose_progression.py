"""propose_progression tool (F2, write-path design per security-expert
HITL review): computes a weight/rep target for an exercise and persists
it as a short-lived, single-use proposal. This is the ONLY way the
actual Hevy write (confirm_progression Lambda, outside the model) can
execute — it takes a proposal_id, never a free-form weight, so the model
can never originate an arbitrary Hevy write value.

The model supplies ONLY the exercise_template_id (which
find_progression_candidate already selected deterministically). The
proposed numbers are always computed here from the user's own logged
history via the +2.5kg double-progression heuristic — the tool
deliberately accepts no weight/reps parameters at all. An earlier
version allowed the model to pass user-requested numbers through (for
the chat flow, e.g. "set my bench to 60kg"); with email-only delivery
there is no chat turn where a user could ask, so that parameter surface
was removed entirely rather than left as an unused write path.

Read-only: never touches the Hevy API, only DynamoDB. Deciding WHETHER a
change is warranted lives in find_progression_candidate, not here; this
tool only computes and records the numbers once asked.
"""
from __future__ import annotations

import os
import time
import uuid
from decimal import Decimal
from typing import Optional

import boto3
from strands import tool

from tools.query_workout_history import fetch_history

_PROGRESSION_INCREMENT_KG = 2.5
_PROPOSAL_TTL_SECONDS = 3 * 24 * 60 * 60  # 3 days — confirmed via emailed link, not same-session chat


def _table():
    table_name = os.environ.get("STATS_TABLE_NAME", "workout-coach-stats")
    region = os.environ.get("AWS_REGION", "eu-west-1")
    return boto3.resource("dynamodb", region_name=region).Table(table_name)


def compute_proposed_weight(current_weight_kg: float) -> float:
    return round(current_weight_kg + _PROGRESSION_INCREMENT_KG, 2)


def find_latest_exercise_entry(history: dict, exercise_template_id: str) -> Optional[dict]:
    """Most recent week's entry for this exercise in the given history
    window, or None if it wasn't logged in that window."""
    for week in reversed(history.get("weeks", [])):
        for exercise in week.get("exercises", []):
            if exercise.get("exercise_template_id") == exercise_template_id:
                return exercise
    return None


@tool
def propose_progression(exercise_template_id: str) -> dict:
    """
    Propose a weight progression for an exercise's next session: the
    +2.5kg double-progression heuristic over the user's most recent
    logged weight, at the same recent mean reps. Call this only with the
    exact exercise_template_id that find_progression_candidate returned —
    never with one you picked yourself. The proposed numbers are computed
    here from logged history; you cannot and should not supply them.

    Args:
        exercise_template_id: The Hevy exercise_template_id to propose a
            progression for, exactly as returned by
            find_progression_candidate.

    Returns:
        Dict with proposal_id, exercise_title, current_weight_kg,
        proposed_weight_kg, reps — include the proposal_id in the
        report's confirm placeholder. The proposal expires in 3 days.
        Dict with an 'error' key if the exercise has no recent weighted
        history.
    """
    history = fetch_history(weeks=1)
    entry = find_latest_exercise_entry(history, exercise_template_id)
    if entry is None or entry.get("max_weight_kg") is None:
        return {"error": f"No recent weighted history found for exercise {exercise_template_id}."}

    current_weight_kg = entry["max_weight_kg"]
    proposed_weight_kg = compute_proposed_weight(current_weight_kg)
    proposed_reps = round(entry.get("mean_reps") or 8)

    proposal_id = str(uuid.uuid4())
    target_user_id = os.environ.get("TARGET_USER_ID", "demo-user")
    now = int(time.time())

    _table().put_item(Item={
        "user_id": target_user_id,
        "stat_type": f"PROPOSAL#{proposal_id}",
        "exercise_template_id": exercise_template_id,
        "exercise_title": entry.get("exercise_title"),
        "current_weight_kg": Decimal(str(current_weight_kg)),
        "proposed_weight_kg": Decimal(str(proposed_weight_kg)),
        "reps": proposed_reps,
        "status": "pending",
        "ttl": now + _PROPOSAL_TTL_SECONDS,
    })

    return {
        "proposal_id": proposal_id,
        "exercise_title": entry.get("exercise_title"),
        "current_weight_kg": current_weight_kg,
        "proposed_weight_kg": proposed_weight_kg,
        "reps": proposed_reps,
    }
