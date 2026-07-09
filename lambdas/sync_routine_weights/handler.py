"""sync_routine_weights Lambda (B5): mechanical, deterministic sync of Hevy
routine target weights to the user's last logged max weight per exercise.

Hevy doesn't write a completed workout's weight back into the routine
template on its own (only if you use Hevy's own "update routine" prompt,
and only for routine-based sessions) — so routine targets drift stale
across freeform/multi-routine logging. This closes that gap.

No LLM in the loop, no domain judgment, no confirmation gate — it mirrors
a number the user already logged, it doesn't originate one. See ADR-007
in CLAUDE.md for why that makes this a different risk class from
apply_progression (the model-facing write, which IS token-gated).

Invocation input:
    {"user_id": "demo-user"}

Env vars:
    STATS_TABLE_NAME, HEVY_API_KEY_PARAM
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

import boto3

logger = logging.getLogger(__name__)

HEVY_BASE_URL = "https://api.hevyapp.com"
LATEST_SK = "LATEST"

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
    """Search the user's routines for one containing this exercise.
    Returns the raw routine dict as Hevy returns it, or None."""
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


def current_target_weight(routine: dict, exercise_template_id: str) -> float | None:
    """The routine's current target weight_kg for this exercise's normal
    (non-warmup) sets, or None if the exercise/sets aren't found or the
    exercise has no weight (e.g. bodyweight/time-based)."""
    for exercise in routine.get("exercises", []):
        if exercise.get("exercise_template_id") != exercise_template_id:
            continue
        for s in exercise.get("sets", []):
            if s.get("type") == "normal" and s.get("weight_kg") is not None:
                return float(s["weight_kg"])
    return None


def build_updated_routine_body(routine: dict, exercise_template_id: str, weight_kg: float) -> dict:
    """Mirrors the actual last-logged weight onto every 'normal' set of the
    given exercise. Reps are left untouched — this syncs weight only, it's
    not a progression decision (see apply_progression for that)."""
    exercises = []
    for exercise in routine["exercises"]:
        sets = []
        for s in exercise["sets"]:
            updated = {k: v for k, v in s.items() if k != "index"}
            if exercise.get("exercise_template_id") == exercise_template_id and updated.get("type") == "normal":
                updated["weight_kg"] = weight_kg
            sets.append(updated)
        exercises.append({
            "exercise_template_id": exercise["exercise_template_id"],
            "notes": exercise.get("notes"),
            "superset_id": exercise.get("superset_id"),
            "sets": sets,
            "rest_seconds": exercise.get("rest_seconds", 0),
        })
    return {"routine": {"title": routine["title"], "exercises": exercises}}


# ---- DynamoDB reader ---------------------------------------------------------

def fetch_latest_exercises(table, user_id: str) -> list[dict]:
    response = table.get_item(Key={"user_id": user_id, "stat_type": LATEST_SK})
    item = response.get("Item")
    if not item:
        return []
    return item.get("exercises", [])


# ---- Sync logic ---------------------------------------------------------------

def sync_exercise(api_key: str, exercise_template_id: str, actual_weight_kg: float) -> dict:
    routine = find_routine_for_exercise(api_key, exercise_template_id)
    if routine is None:
        return {"exercise_template_id": exercise_template_id, "status": "no_routine_found"}

    target_weight_kg = current_target_weight(routine, exercise_template_id)
    if target_weight_kg is not None and abs(target_weight_kg - actual_weight_kg) < 0.01:
        return {"exercise_template_id": exercise_template_id, "status": "already_in_sync"}

    body = build_updated_routine_body(routine, exercise_template_id, actual_weight_kg)
    _request("PUT", f"/v1/routines/{routine['id']}", api_key, body=body)
    return {
        "exercise_template_id": exercise_template_id,
        "status": "updated",
        "previous_weight_kg": target_weight_kg,
        "new_weight_kg": actual_weight_kg,
    }


def sync(user_id: str) -> dict:
    table_name = os.environ["STATS_TABLE_NAME"]
    table = boto3.resource("dynamodb").Table(table_name)

    exercises = fetch_latest_exercises(table, user_id)
    if not exercises:
        return {"user_id": user_id, "exercises_checked": 0, "updated": 0, "results": []}

    ssm_client = boto3.client("ssm")
    api_key = get_api_key(ssm_client, os.environ["HEVY_API_KEY_PARAM"])

    results = []
    for exercise in exercises:
        exercise_template_id = exercise.get("exercise_template_id")
        max_weight_kg = exercise.get("max_weight_kg")
        if exercise_template_id is None or max_weight_kg is None:
            continue
        try:
            result = sync_exercise(api_key, exercise_template_id, float(max_weight_kg))
        except HevyAPIError as exc:
            logger.warning("Sync failed for %s: %s", exercise_template_id, exc)
            result = {"exercise_template_id": exercise_template_id, "status": "error", "error": str(exc)}
        results.append(result)

    return {
        "user_id": user_id,
        "exercises_checked": len(results),
        "updated": sum(1 for r in results if r["status"] == "updated"),
        "results": results,
    }


def handler(event, _context):
    return sync(event["user_id"])
