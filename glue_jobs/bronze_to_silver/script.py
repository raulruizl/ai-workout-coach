"""Glue Python Shell job (B2): bronze -> silver.

Reads new bronze objects (raw Hevy event batches) for a user, flattens each
workout to one row per set, and writes parquet to the silver zone. Silver
never drops rows — every set from an "updated" event lands, including
warmups (flagged via is_warmup, not filtered). "deleted" events go to a
separate deleted_workouts dataset so B3 (silver->gold) can exclude those
workout_ids from aggregation without silver itself losing history.

Dedup/upsert across runs is NOT done here — plain parquet (ADR-003, no
Iceberg yet) can't cheaply rewrite prior files. Each row carries
_ingested_at so downstream consumers (B3) can pick the latest version of a
given (workout_id, exercise_template_id, set_index) themselves.

Env vars:
    BRONZE_BUCKET, SILVER_BUCKET, STATS_TABLE_NAME
"""
import gzip
import io
import json
import logging
import os
import sys

import boto3
import pandas as pd

logger = logging.getLogger(__name__)

SILVER_CURSOR_SK = "SILVER_TRANSFORM_CURSOR"
SCHEMA_VERSION = 1

SET_COLUMNS = [
    "user_id", "workout_id", "workout_date", "exercise_template_id", "exercise_title",
    "set_index", "set_type", "is_warmup", "weight_kg", "reps", "distance_meters",
    "duration_seconds", "rpe", "schema_version", "_ingested_at",
]
DELETED_COLUMNS = ["user_id", "workout_id", "deleted_at", "schema_version", "_ingested_at"]


# ---- Bronze discovery --------------------------------------------------------

def list_bronze_keys(s3_client, bucket: str, user_id: str) -> list[str]:
    """All bronze object keys for a user, oldest-to-newest (run_id sorts lexically)."""
    prefix = f"workouts/user_id={user_id}/"
    keys = []
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            keys.append(obj["Key"])
    return sorted(keys)


def unprocessed_keys(keys: list[str], cursor_run_id: str) -> list[str]:
    """Bronze keys are workouts/user_id=<id>/ingest_date=<date>/run_<run_id>.json.gz —
    run_id is a sortable UTC timestamp string, so a plain string compare works."""
    return [key for key in keys if _run_id_from_key(key) > cursor_run_id]


def _run_id_from_key(key: str) -> str:
    filename = key.rsplit("/", 1)[-1]  # run_<run_id>.json.gz
    return filename.removeprefix("run_").removesuffix(".json.gz")


def read_bronze_object(s3_client, bucket: str, key: str) -> dict:
    obj = s3_client.get_object(Bucket=bucket, Key=key)
    body = gzip.decompress(obj["Body"].read())
    return json.loads(body)


# ---- Flatten -----------------------------------------------------------------

def flatten_workout_to_set_rows(user_id: str, workout: dict, ingested_at: str) -> list[dict]:
    workout_date = workout["start_time"][:10]  # ISO 8601 date prefix
    rows = []
    for exercise in workout.get("exercises", []):
        for s in exercise.get("sets", []):
            rows.append({
                "user_id": user_id,
                "workout_id": workout["id"],
                "workout_date": workout_date,
                "exercise_template_id": exercise.get("exercise_template_id"),
                "exercise_title": exercise.get("title"),
                "set_index": s.get("index"),
                "set_type": s.get("type"),
                "is_warmup": s.get("type") == "warmup",
                "weight_kg": s.get("weight_kg"),
                "reps": s.get("reps"),
                "distance_meters": s.get("distance_meters"),
                "duration_seconds": s.get("duration_seconds"),
                "rpe": s.get("rpe"),
                "schema_version": SCHEMA_VERSION,
                "_ingested_at": ingested_at,
            })
    return rows


def process_events(user_id: str, events: list[dict], ingested_at: str) -> tuple[list[dict], list[dict]]:
    """Returns (set_rows, deleted_workout_rows)."""
    set_rows = []
    deleted_rows = []

    for event in events:
        if event.get("type") == "updated":
            set_rows.extend(flatten_workout_to_set_rows(user_id, event["workout"], ingested_at))
        elif event.get("type") == "deleted":
            deleted_rows.append({
                "user_id": user_id,
                "workout_id": event["id"],
                "deleted_at": event["deleted_at"],
                "schema_version": SCHEMA_VERSION,
                "_ingested_at": ingested_at,
            })

    return set_rows, deleted_rows


# ---- Silver writer -------------------------------------------------------------

def build_silver_key(dataset: str, user_id: str, year_month: str, run_id: str) -> str:
    return f"{dataset}/user_id={user_id}/year_month={year_month}/run_{run_id}.parquet"


def group_by_year_month(rows: list[dict], date_field: str) -> dict[str, list[dict]]:
    """A single bronze batch can span multiple months — partition correctly
    instead of assuming everything belongs to one month."""
    groups: dict[str, list[dict]] = {}
    for row in rows:
        year_month = row[date_field][:7]
        groups.setdefault(year_month, []).append(row)
    return groups


def write_parquet(s3_client, bucket: str, key: str, rows: list[dict], columns: list[str]) -> None:
    df = pd.DataFrame(rows, columns=columns)
    buffer = io.BytesIO()
    df.to_parquet(buffer, engine="pyarrow", index=False)
    s3_client.put_object(Bucket=bucket, Key=key, Body=buffer.getvalue())


# ---- Cursor (DynamoDB, same table/pattern as B1) --------------------------------

def get_cursor(table, user_id: str) -> str:
    response = table.get_item(Key={"user_id": user_id, "stat_type": SILVER_CURSOR_SK})
    item = response.get("Item")
    return item["run_id"] if item else ""


def set_cursor(table, user_id: str, run_id: str) -> None:
    table.put_item(Item={"user_id": user_id, "stat_type": SILVER_CURSOR_SK, "run_id": run_id})


# ---- Entrypoint ------------------------------------------------------------------

def run(user_id: str) -> dict:
    bronze_bucket = os.environ["BRONZE_BUCKET"]
    silver_bucket = os.environ["SILVER_BUCKET"]
    table_name = os.environ["STATS_TABLE_NAME"]

    s3_client = boto3.client("s3")
    table = boto3.resource("dynamodb").Table(table_name)

    cursor = get_cursor(table, user_id)
    keys = unprocessed_keys(list_bronze_keys(s3_client, bronze_bucket, user_id), cursor)

    result = {"user_id": user_id, "bronze_objects_processed": 0, "set_rows_written": 0, "deleted_rows_written": 0}
    latest_run_id = cursor

    for key in keys:
        payload = read_bronze_object(s3_client, bronze_bucket, key)
        run_id = _run_id_from_key(key)
        ingested_at = payload["_ingested_at"]

        set_rows, deleted_rows = process_events(user_id, payload["events"], ingested_at)

        for year_month, rows in group_by_year_month(set_rows, "workout_date").items():
            silver_key = build_silver_key("sets", user_id, year_month, run_id)
            write_parquet(s3_client, silver_bucket, silver_key, rows, SET_COLUMNS)
            result["set_rows_written"] += len(rows)

        for year_month, rows in group_by_year_month(deleted_rows, "deleted_at").items():
            deleted_key = build_silver_key("deleted_workouts", user_id, year_month, run_id)
            write_parquet(s3_client, silver_bucket, deleted_key, rows, DELETED_COLUMNS)
            result["deleted_rows_written"] += len(rows)

        result["bronze_objects_processed"] += 1
        latest_run_id = max(latest_run_id, run_id)

    if latest_run_id != cursor:
        set_cursor(table, user_id, latest_run_id)

    return result


def apply_cli_args_as_env(argv: list[str]) -> None:
    """Glue Python Shell passes job parameters as --KEY value CLI args, not OS
    env vars — translate them so run() stays testable/portable across both
    a local invoke (env vars pre-set) and a real Glue job run."""
    i = 0
    while i < len(argv) - 1:
        if argv[i].startswith("--"):
            os.environ.setdefault(argv[i][2:], argv[i + 1])
            i += 2
        else:
            i += 1


if __name__ == "__main__":
    apply_cli_args_as_env(sys.argv[1:])
    target_user = os.environ.get("TARGET_USER_ID", "demo-user")
    print(run(target_user))
