"""confirm_progression Lambda: the actual write execution behind the
one-click confirm link in the weekly report email.

Deliberately NOT invoked through the agent/model — apply_progression used
to be a Strands tool the model could call, but there's no chat turn left
to confirm in (email-only delivery), so the model shouldn't hold the
Hevy write credential at all anymore. This Lambda duplicates that same
deterministic logic standalone: claim a proposal_id (single-use,
conditional DynamoDB update — the actual security boundary) and PUT the
proposed weight/reps to the user's Hevy routine. The proposal_id itself
is the bearer token; anyone with the link can apply it once, before it
expires — same trust model as any single-use emailed confirm link.

Exposed via a public Lambda Function URL (AuthType NONE) since it's
clicked directly from an email client, not called with signed AWS creds.

Invocation: Function URL GET/POST with ?proposal_id=<uuid>

Env vars:
    STATS_TABLE_NAME, HEVY_API_KEY_PARAM, TARGET_USER_ID
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Optional

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

HEVY_BASE_URL = "https://api.hevyapp.com"

_api_key_cache = None


class HevyAPIError(Exception):
    """Raised on any non-2xx response or transport failure."""


# ---- Hevy API client --------------------------------------------------------

def get_api_key(ssm_client, parameter_name: str) -> str:
    global _api_key_cache
    if _api_key_cache is None:
        response = ssm_client.get_parameter(Name=parameter_name, WithDecryption=True)
        _api_key_cache = response["Parameter"]["Value"]
    return _api_key_cache


def _request(method: str, path: str, api_key: str, body: dict | None = None) -> dict:
    url = f"{HEVY_BASE_URL}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"api-key": api_key, "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise HevyAPIError(f"Hevy API {method} {path} returned {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise HevyAPIError(f"Hevy API unreachable for {method} {path}: {exc.reason}") from exc


def find_routine_for_exercise(api_key: str, exercise_template_id: str) -> dict | None:
    page = 1
    page_count = 1
    while page <= page_count:
        envelope = _request("GET", f"/v1/routines?page={page}&pageSize=10", api_key)
        for routine in envelope.get("routines", []):
            for exercise in routine.get("exercises", []):
                if exercise.get("exercise_template_id") == exercise_template_id:
                    return routine
        page_count = envelope.get("page_count", 1)
        page += 1
    return None


def build_updated_routine_body(routine: dict, exercise_template_id: str,
                                weight_kg: float, reps: int) -> dict:
    exercises = []
    for exercise in routine["exercises"]:
        sets = []
        for s in exercise["sets"]:
            updated = {k: v for k, v in s.items() if k != "index"}
            if exercise.get("exercise_template_id") == exercise_template_id and updated.get("type") == "normal":
                updated["weight_kg"] = weight_kg
                updated["reps"] = reps
            sets.append(updated)
        exercises.append({
            "exercise_template_id": exercise["exercise_template_id"],
            "notes": exercise.get("notes"),
            "superset_id": exercise.get("superset_id"),
            "sets": sets,
            "rest_seconds": exercise.get("rest_seconds", 0),
        })
    return {"routine": {"title": routine["title"], "exercises": exercises}}


def update_exercise_target(api_key: str, routine: dict, exercise_template_id: str,
                            weight_kg: float, reps: int) -> dict:
    body = build_updated_routine_body(routine, exercise_template_id, weight_kg, reps)
    return _request("PUT", f"/v1/routines/{routine['id']}", api_key, body=body)


# ---- Proposal claim (DynamoDB, single-use, TTL-bound) --------------------------

def _table():
    table_name = os.environ.get("STATS_TABLE_NAME", "workout-coach-stats")
    return boto3.resource("dynamodb").Table(table_name)


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


# ---- HTML responses -------------------------------------------------------------

def _html_response(status_code: int, title: str, message: str) -> dict:
    body = f"""<!doctype html><html><head><meta charset="utf-8"><title>{title}</title></head>
<body style="font-family: sans-serif; max-width: 480px; margin: 4rem auto; text-align: center;">
<h2>{title}</h2><p>{message}</p></body></html>"""
    return {"statusCode": status_code, "headers": {"Content-Type": "text/html"}, "body": body}


# ---- Entrypoint ------------------------------------------------------------------

def confirm(proposal_id: str) -> dict:
    target_user_id = os.environ.get("TARGET_USER_ID", "demo-user")
    table = _table()

    proposal = claim_proposal(table, target_user_id, proposal_id)
    if proposal is None:
        return _html_response(410, "Link expired", "This proposal was already applied, expired, or doesn't exist.")

    ssm_client = boto3.client("ssm")
    api_key = get_api_key(ssm_client, os.environ["HEVY_API_KEY_PARAM"])

    exercise_template_id = proposal["exercise_template_id"]
    routine = find_routine_for_exercise(api_key, exercise_template_id)
    if routine is None:
        return _html_response(404, "Routine not found",
                               f"Could not find a routine containing exercise {exercise_template_id}.")

    weight_kg = float(proposal["proposed_weight_kg"])
    reps = int(proposal["reps"])
    try:
        update_exercise_target(api_key, routine, exercise_template_id, weight_kg=weight_kg, reps=reps)
    except HevyAPIError as exc:
        logger.error("Hevy write failed for proposal %s: %s", proposal_id, exc)
        return _html_response(502, "Update failed", "Couldn't reach Hevy right now — try again shortly.")

    exercise_title = proposal.get("exercise_title", exercise_template_id)
    return _html_response(200, "Updated!", f"{exercise_title} set to {weight_kg}kg x {reps} for next session.")


def handler(event, _context):
    params = event.get("queryStringParameters") or {}
    proposal_id = params.get("proposal_id")
    if not proposal_id:
        return _html_response(400, "Missing proposal", "No proposal_id in the link.")
    return confirm(proposal_id)
