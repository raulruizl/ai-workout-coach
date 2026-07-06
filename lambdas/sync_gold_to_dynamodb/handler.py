"""Sync Lambda (B4): gold S3 parquet -> DynamoDB serving layer.

Reads every weekly_summary + weekly_exercise_stats gold partition for a
user (both written fresh each B3 run) and upserts one DynamoDB item per
week (SK=WEEK#<date>), plus a SK=LATEST item mirroring the most recent
week. This is the sole serving layer the agent reads (ADR-004) — Gold
parquet in S3 exists for lineage/archival only, read solely by this
Lambda.

Requires pandas/pyarrow, provided via the AWSSDKPandas Lambda layer (not
bundled in the deployment zip) — see terraform/lambda_sync_gold.tf.

Invocation input (from Step Functions or a test invoke):
    {"user_id": "demo-user"}

Env vars:
    GOLD_BUCKET, STATS_TABLE_NAME
"""
import io
import logging
import os
from decimal import Decimal

import boto3
import pandas as pd

logger = logging.getLogger(__name__)

LATEST_SK = "LATEST"


# ---- Gold readers -----------------------------------------------------------

def list_gold_keys(s3_client, bucket: str, dataset: str, user_id: str) -> list[str]:
    prefix = f"{dataset}/user_id={user_id}/"
    keys = []
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            keys.append(obj["Key"])
    return keys


def read_parquet_object(s3_client, bucket: str, key: str) -> pd.DataFrame:
    obj = s3_client.get_object(Bucket=bucket, Key=key)
    return pd.read_parquet(io.BytesIO(obj["Body"].read()))


def load_gold_dataset(s3_client, bucket: str, dataset: str, user_id: str) -> pd.DataFrame:
    keys = list_gold_keys(s3_client, bucket, dataset, user_id)
    if not keys:
        return pd.DataFrame()
    frames = [read_parquet_object(s3_client, bucket, key) for key in keys]
    return pd.concat(frames, ignore_index=True)


# ---- DynamoDB item construction ----------------------------------------------

def to_number(value):
    """None/NaN -> None (DynamoDB rejects NaN); everything else -> Decimal,
    since boto3's resource API requires Decimal for numeric attributes."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    return Decimal(str(value))


def build_exercise_items(exercise_rows: pd.DataFrame) -> list[dict]:
    items = []
    for _, row in exercise_rows.iterrows():
        items.append({
            "exercise_template_id": row["exercise_template_id"],
            "exercise_title": row["exercise_title"],
            "total_volume_kg": to_number(row["total_volume_kg"]),
            "max_weight_kg": to_number(row["max_weight_kg"]),
            "mean_reps": to_number(row["mean_reps"]),
            "best_est_1rm": to_number(row["best_est_1rm"]),
            "set_count": int(row["set_count"]),
        })
    return items


def build_week_item(user_id: str, week: str, stat_type: str,
                     summary_row: pd.Series, exercise_rows: pd.DataFrame) -> dict:
    return {
        "user_id": user_id,
        "stat_type": stat_type,
        "week": week,
        "total_volume_kg": to_number(summary_row["total_volume_kg"]),
        "workout_count": int(summary_row["workout_count"]),
        "total_sets": int(summary_row["total_sets"]),
        "exercises": build_exercise_items(exercise_rows),
    }


def build_all_items(user_id: str, summary_df: pd.DataFrame, exercise_df: pd.DataFrame) -> list[dict]:
    """One WEEK#<date> item per week present in weekly_summary, plus a
    LATEST item mirroring the most recent week."""
    if summary_df.empty:
        return []

    items = []
    for _, summary_row in summary_df.iterrows():
        week = summary_row["week"]
        exercise_rows = exercise_df[exercise_df["week"] == week] if not exercise_df.empty else exercise_df
        items.append(build_week_item(user_id, week, f"WEEK#{week}", summary_row, exercise_rows))

    latest_summary = summary_df.loc[summary_df["week"].idxmax()]
    latest_week = latest_summary["week"]
    latest_exercise_rows = exercise_df[exercise_df["week"] == latest_week] if not exercise_df.empty else exercise_df
    items.append(build_week_item(user_id, latest_week, LATEST_SK, latest_summary, latest_exercise_rows))

    return items


def write_items(table, items: list[dict]) -> int:
    written = 0
    with table.batch_writer() as batch:
        for item in items:
            batch.put_item(Item=item)
            written += 1
    return written


# ---- Entrypoint ------------------------------------------------------------------

def sync(user_id: str) -> dict:
    gold_bucket = os.environ["GOLD_BUCKET"]
    table_name = os.environ["STATS_TABLE_NAME"]

    s3_client = boto3.client("s3")
    table = boto3.resource("dynamodb").Table(table_name)

    summary_df = load_gold_dataset(s3_client, gold_bucket, "weekly_summary", user_id)
    exercise_df = load_gold_dataset(s3_client, gold_bucket, "weekly_exercise_stats", user_id)

    items = build_all_items(user_id, summary_df, exercise_df)
    written = write_items(table, items)

    return {"user_id": user_id, "weeks_found": len(summary_df), "items_written": written}


def handler(event, _context):
    return sync(event["user_id"])
