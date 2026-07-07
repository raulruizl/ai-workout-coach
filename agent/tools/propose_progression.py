"""propose_progression tool (F2, write-path design per security-expert
HITL review): computes a weight/rep target for an exercise and persists
it as a short-lived, single-use proposal. This is the ONLY way
apply_progression can execute a write — it takes a proposal_id, never a
free-form weight, so the model can never originate an arbitrary Hevy
write value directly.

Two ways to get a proposed weight/reps:
- Omit weight_kg/reps: deterministic +2.5kg heuristic off the user's own
  logged history (the original F2 behavior, for model-initiated
  progression suggestions after spotting a plateau etc).
- Pass weight_kg/reps explicitly: the user's own directly-requested
  numbers (e.g. "set my next bench to 60kg for 8 reps"), bounds-checked
  here before being staged as a proposal. Still requires the same
  explicit-confirmation + apply_progression(proposal_id) round trip as
  the heuristic path — a user asking for a number in chat doesn't skip
  the gate, it just supplies the number instead of the heuristic.

Read-only: never touches the Hevy API, only DynamoDB. Deciding WHETHER a
change is warranted (plateau, user request, etc.) is the calling agent's
domain judgment, not this tool's; it only validates and records the
numbers once asked.
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
_MAX_WEIGHT_KG = 500  # sanity bound on user-supplied weight, not a real training limit
_MAX_REPS = 50  # sanity bound on user-supplied reps


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
def propose_progression(exercise_template_id: str, weight_kg: Optional[float] = None,
                         reps: Optional[int] = None) -> dict:
    """
    Propose a weight/rep target for an exercise's next session, based on
    the user's own logged history. Two call shapes:
    - No weight_kg/reps: computes the deterministic +2.5kg heuristic —
      call this only after you've identified a real reason to progress
      (e.g. a plateau or consistent performance at the current weight).
    - weight_kg and/or reps given: use these when the user directly asks
      for a specific number (e.g. "set my next bench to 60kg for 8
      reps") — pass their exact numbers through rather than guessing.
    Either way, this tool doesn't decide whether the change is warranted,
    it only validates and records the proposed numbers.

    Args:
        exercise_template_id: The Hevy exercise_template_id to update,
            as seen in get_latest_stats/query_workout_history output.
        weight_kg: Optional user-requested target weight in kg. Omit to
            use the +2.5kg-over-current heuristic instead.
        reps: Optional user-requested target rep count. Omit to use the
            exercise's recent mean reps instead.

    Returns:
        Dict with proposal_id, exercise_title, current_weight_kg,
        proposed_weight_kg, reps — relay this to the user and ask for
        explicit confirmation before calling apply_progression with the
        same proposal_id. The proposal expires in 10 minutes.
        Dict with an 'error' key if the exercise has no recent history,
        or if weight_kg/reps is outside a sane range.
    """
    if weight_kg is not None and not (0 < weight_kg <= _MAX_WEIGHT_KG):
        return {"error": f"weight_kg must be between 0 and {_MAX_WEIGHT_KG}."}
    if reps is not None and not (1 <= reps <= _MAX_REPS):
        return {"error": f"reps must be between 1 and {_MAX_REPS}."}

    history = fetch_history(weeks=1)
    entry = find_latest_exercise_entry(history, exercise_template_id)
    if entry is None or entry.get("max_weight_kg") is None:
        return {"error": f"No recent weighted history found for exercise {exercise_template_id}."}

    current_weight_kg = entry["max_weight_kg"]
    proposed_weight_kg = round(weight_kg, 2) if weight_kg is not None else compute_proposed_weight(current_weight_kg)
    proposed_reps = reps if reps is not None else round(entry.get("mean_reps") or 8)

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
