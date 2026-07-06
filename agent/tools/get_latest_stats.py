"""get_latest_stats tool (F2, first cut): reads the SK=LATEST item the sole
serving layer (DynamoDB) holds for the user, written by B4. Deterministic
data-fetch only — no domain judgment here, that lives in the agent's
system prompt.

Single-user, no-auth system today (CLAUDE.md) — user_id is never taken
from the LLM/agent-supplied input, it's fixed via TARGET_USER_ID. This
sidesteps "never string-interpolate agent-supplied values into DynamoDB
key construction" entirely rather than validating it.
"""
from __future__ import annotations

import os
from decimal import Decimal

import boto3
from strands import tool

def _table():
    table_name = os.environ.get("STATS_TABLE_NAME", "workout-coach-stats")
    region = os.environ.get("AWS_REGION", "eu-west-1")
    return boto3.resource("dynamodb", region_name=region).Table(table_name)


def to_plain(value):
    """Decimal -> int/float so tool output is normal JSON-friendly Python,
    not boto3's DynamoDB-specific numeric type."""
    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    if isinstance(value, list):
        return [to_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: to_plain(v) for k, v in value.items()}
    return value


@tool
def get_latest_stats() -> dict:
    """
    Get the user's most recent week of training stats: total volume,
    workout count, set count, and per-exercise breakdown (volume, max
    weight, mean reps, estimated 1RM).

    Returns:
        Dict with week, total_volume_kg, workout_count, total_sets, and
        an 'exercises' list — or a dict with an 'error' key if no data
        has been synced yet.
    """
    target_user_id = os.environ.get("TARGET_USER_ID", "demo-user")
    response = _table().get_item(Key={"user_id": target_user_id, "stat_type": "LATEST"})
    item = response.get("Item")
    if not item:
        return {"error": "No stats found yet — the pipeline may not have run for this user."}
    return to_plain({k: v for k, v in item.items() if k not in ("user_id", "stat_type")})
