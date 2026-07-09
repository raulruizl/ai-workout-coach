"""query_workout_history tool (F2): reads a range of SK=WEEK#<date> items
from the sole serving layer (DynamoDB), written by B4. Deterministic
data-fetch only — trend interpretation lives in the agent's system prompt.

Single-user, no-auth pattern: user_id is fixed via TARGET_USER_ID, never
agent-supplied. This sidesteps "never string-interpolate agent-supplied
values into DynamoDB key construction" entirely rather than validating it.
"""
from __future__ import annotations

import os
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key
from strands import tool

_MAX_WEEKS = 52


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


def clamp_weeks(weeks: int) -> int:
    return max(1, min(weeks, _MAX_WEEKS))


def fetch_history(weeks: int = 4) -> dict:
    """Plain (undecorated) implementation, importable by other tools
    (e.g. propose_progression) without going through the @tool wrapper."""
    limit = clamp_weeks(weeks)
    target_user_id = os.environ.get("TARGET_USER_ID", "demo-user")

    response = _table().query(
        KeyConditionExpression=Key("user_id").eq(target_user_id) & Key("stat_type").begins_with("WEEK#"),
        ScanIndexForward=False,
        Limit=limit,
    )
    items = response.get("Items", [])
    items.reverse()  # oldest -> newest, easier for trend reasoning

    return {
        "weeks": [
            to_plain({k: v for k, v in item.items() if k not in ("user_id", "stat_type")})
            for item in items
        ]
    }


@tool
def query_workout_history(weeks: int = 4) -> dict:
    """
    Get the user's training stats across their most recent N weeks, oldest
    first — use this for any question about trends, progression, or
    plateaus that a single week's data can't answer.

    Args:
        weeks: How many of the most recent weeks to return (1-52). Default
            4 — the fixed analysis window for the weekly report; only pass
            a larger value if explicitly asked for a longer range.

    Returns:
        Dict with a 'weeks' list (oldest to newest), each item with week
        date, total_volume_kg, workout_count, total_sets, and exercises[].
        Empty list if no history has been synced yet.
    """
    return fetch_history(weeks)
