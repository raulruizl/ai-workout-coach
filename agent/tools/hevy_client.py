"""Shared Hevy API client helpers for the write-capable agent tool
(apply_progression). propose_progression never imports this — it only
reads DynamoDB — keeping the Hevy API key credential scoped to the one
tool that actually needs it.

Schema verified against the real Hevy API (2026-07-06), not assumed —
per CLAUDE.md's working agreement to confirm real API shape before
building against it. Two write-schema quirks found by trial:
- Sets must not include 'index' (positional order in the array = index).
- Exercises must not include 'title' (derived server-side from
  exercise_template_id; sending it back is rejected as invalid).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

HEVY_BASE_URL = "https://api.hevyapp.com"

_api_key_cache = None


class HevyAPIError(Exception):
    """Raised on any non-2xx response or transport failure."""


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


def build_updated_routine_body(routine: dict, exercise_template_id: str,
                                weight_kg: float, reps: int) -> dict:
    """Sets weight_kg/reps on every 'normal' (non-warmup) set of the given
    exercise, strips write-rejected fields from the rest, and returns the
    full PUT request body Hevy expects."""
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
