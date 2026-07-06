"""apply_progression tool (F2, write-path): the ONLY write-capable agent
tool. Executes a weight-progression write to the user's Hevy routine, but
ONLY against a proposal_id previously minted by propose_progression — the
model cannot supply an arbitrary weight itself.

Proposals are single-use (conditional DynamoDB update prevents replay)
and expire after 10 minutes (DynamoDB TTL, checked again here since TTL
deletion isn't instantaneous). This is the only tool holding the Hevy API
key credential — propose_progression never touches it.
"""
from __future__ import annotations

import os
import time
from typing import Optional

import boto3
from botocore.exceptions import ClientError
from strands import tool

from tools.hevy_client import HevyAPIError, find_routine_for_exercise, get_api_key, update_exercise_target


def _table():
    table_name = os.environ.get("STATS_TABLE_NAME", "workout-coach-stats")
    region = os.environ.get("AWS_REGION", "eu-west-1")
    return boto3.resource("dynamodb", region_name=region).Table(table_name)


def claim_proposal(table, user_id: str, proposal_id: str) -> Optional[dict]:
    """Atomically flips a pending, unexpired proposal to 'applied' so it
    can never be replayed. Returns the claimed item, or None if it was
    missing, already applied, or expired."""
    try:
        response = table.update_item(
            Key={"user_id": user_id, "stat_type": f"PROPOSAL#{proposal_id}"},
            UpdateExpression="SET #s = :applied",
            ConditionExpression="attribute_exists(#s) AND #s = :pending AND #ttl > :now",
            ExpressionAttributeNames={"#s": "status", "#ttl": "ttl"},
            ExpressionAttributeValues={":applied": "applied", ":pending": "pending", ":now": int(time.time())},
            ReturnValues="ALL_NEW",
        )
        return response["Attributes"]
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return None
        raise


@tool
def apply_progression(proposal_id: str) -> dict:
    """
    Apply a previously proposed weight progression to the user's Hevy
    routine. Only call this after the user has explicitly confirmed the
    exact proposal (weight/reps) that propose_progression returned — never
    call this speculatively or without confirmation.

    Args:
        proposal_id: The proposal_id returned by a prior propose_progression call.

    Returns:
        Dict with the applied exercise_title, weight_kg, reps on success,
        or a dict with an 'error' key if the proposal is missing, already
        applied, or expired — in which case ask for a fresh proposal.
    """
    target_user_id = os.environ.get("TARGET_USER_ID", "demo-user")
    region = os.environ.get("AWS_REGION", "eu-west-1")
    table = _table()

    proposal = claim_proposal(table, target_user_id, proposal_id)
    if proposal is None:
        return {"error": "Proposal not found, already applied, or expired. Ask for a fresh proposal."}

    ssm_client = boto3.client("ssm", region_name=region)
    api_key = get_api_key(ssm_client, os.environ["HEVY_API_KEY_PARAM"])

    exercise_template_id = proposal["exercise_template_id"]
    routine = find_routine_for_exercise(api_key, exercise_template_id)
    if routine is None:
        return {"error": f"Could not find a routine containing exercise {exercise_template_id}."}

    try:
        update_exercise_target(
            api_key, routine, exercise_template_id,
            weight_kg=float(proposal["proposed_weight_kg"]), reps=int(proposal["reps"]),
        )
    except HevyAPIError as exc:
        return {"error": str(exc)}

    return {
        "exercise_title": proposal.get("exercise_title"),
        "weight_kg": float(proposal["proposed_weight_kg"]),
        "reps": int(proposal["reps"]),
    }
