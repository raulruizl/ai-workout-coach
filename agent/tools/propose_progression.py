"""propose_progression tool (F2, write-path design per security-expert
HITL review): computes a deterministic weight-progression suggestion
from the user's own logged history and persists it as a short-lived,
single-use proposal. This is the ONLY way apply_progression can execute
a write — it takes a proposal_id, never a free-form weight, so the model
can never originate an arbitrary Hevy write value.

Read-only: never touches the Hevy API, only DynamoDB. Deterministic math
only — deciding WHETHER progression is warranted (plateau, consistent
performance, etc.) is the calling agent's domain judgment, not this
tool's; it only computes the numbers once asked.
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
_PROPOSAL_TTL_SECONDS = 600  # 10 minutes


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
    Propose a weight increase for an exercise, based on the user's own
    logged history. Call this only after you've identified a real reason
    to progress (e.g. a plateau or consistent performance at the current
    weight) — this tool doesn't decide whether progression is warranted,
    it only computes and records the proposed numbers.

    Args:
        exercise_template_id: The Hevy exercise_template_id to progress,
            as seen in get_latest_stats/query_workout_history output.

    Returns:
        Dict with proposal_id, exercise_title, current_weight_kg,
        proposed_weight_kg, reps — relay this to the user and ask for
        explicit confirmation before calling apply_progression with the
        same proposal_id. The proposal expires in 10 minutes.
        Dict with an 'error' key if the exercise has no recent history.
    """
    history = fetch_history(weeks=1)
    entry = find_latest_exercise_entry(history, exercise_template_id)
    if entry is None or entry.get("max_weight_kg") is None:
        return {"error": f"No recent weighted history found for exercise {exercise_template_id}."}

    current_weight_kg = entry["max_weight_kg"]
    reps = round(entry.get("mean_reps") or 8)
    proposed_weight_kg = compute_proposed_weight(current_weight_kg)

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
        "reps": reps,
        "status": "pending",
        "ttl": now + _PROPOSAL_TTL_SECONDS,
    })

    return {
        "proposal_id": proposal_id,
        "exercise_title": entry.get("exercise_title"),
        "current_weight_kg": current_weight_kg,
        "proposed_weight_kg": proposed_weight_kg,
        "reps": reps,
    }
