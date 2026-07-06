"""Glue Python Shell job (B3): silver -> gold.

Silver (B2) is plain parquet, append-only, undeduped (ADR-003, no Iceberg
yet) — every bronze run writes new files, and the same
(workout_id, exercise_template_id, set_index) can appear more than once
across runs if Hevy sent an "updated" event for an already-seen workout.
So B3 does a full recompute on every run: read all silver sets + deleted
markers for the user, dedup, drop deleted workouts, apply the warmup
exclusion, and rewrite the gold weekly partitions from scratch. Gold is a
derived cache (ADR-004 note: DynamoDB is the sole *serving* layer; gold
parquet exists for lineage/archival, read only by B4) — idempotent
overwrite per (user_id, week) partition is simpler and cheap at this data
volume (KB-MB/day) than incremental upsert logic.

Stall detection is NOT computed here — it's an agent tool (F2, detect_stall)
that reads DynamoDB history on demand. Keeps that domain judgment out of
the pipeline, consistent with "no domain judgment in transform code."

Env vars:
    SILVER_BUCKET, GOLD_BUCKET
"""
import datetime
import io
import logging
import os
import sys
from typing import Optional

import boto3
import pandas as pd

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
MAX_REPS_FOR_1RM = 12

EXERCISE_STATS_COLUMNS = [
    "user_id", "week", "exercise_template_id", "exercise_title",
    "total_volume_kg", "max_weight_kg", "mean_reps", "best_est_1rm", "set_count",
    "total_distance_meters", "total_duration_seconds", "schema_version",
]
SUMMARY_COLUMNS = [
    "user_id", "week", "total_volume_kg", "workout_count", "total_sets", "schema_version",
]


# ---- Silver readers -----------------------------------------------------------

def list_silver_keys(s3_client, bucket: str, dataset: str, user_id: str) -> list[str]:
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


def load_all(s3_client, bucket: str, dataset: str, user_id: str, columns: list[str]) -> pd.DataFrame:
    keys = list_silver_keys(s3_client, bucket, dataset, user_id)
    if not keys:
        return pd.DataFrame(columns=columns)
    frames = [read_parquet_object(s3_client, bucket, key) for key in keys]
    return pd.concat(frames, ignore_index=True)


# ---- Dedup / delete exclusion --------------------------------------------------

def dedup_sets(df: pd.DataFrame) -> pd.DataFrame:
    """Keep the most-recently-ingested version of each (workout_id,
    exercise_template_id, set_index) — later bronze events supersede earlier
    ones for the same set."""
    if df.empty:
        return df
    sorted_df = df.sort_values("_ingested_at")
    return sorted_df.drop_duplicates(
        subset=["workout_id", "exercise_template_id", "set_index"], keep="last"
    )


def exclude_deleted_workouts(sets_df: pd.DataFrame, deleted_df: pd.DataFrame) -> pd.DataFrame:
    if deleted_df.empty or sets_df.empty:
        return sets_df
    deleted_ids = set(deleted_df["workout_id"].unique())
    return sets_df[~sets_df["workout_id"].isin(deleted_ids)]


# ---- Domain math ----------------------------------------------------------------

def estimate_1rm(weight_kg, reps) -> Optional[float]:
    """Epley formula. Unreliable above ~12 reps — return null rather than
    compute a misleading number (CLAUDE.md testing rule)."""
    if weight_kg is None or reps is None or pd.isna(weight_kg) or pd.isna(reps):
        return None
    if reps <= 0 or reps > MAX_REPS_FOR_1RM:
        return None
    return weight_kg * (1 + reps / 30)


def week_start(date_str: str) -> str:
    """Monday of the ISO week containing date_str, as YYYY-MM-DD."""
    date = datetime.date.fromisoformat(date_str)
    return (date - datetime.timedelta(days=date.isoweekday() - 1)).isoformat()


# ---- Aggregation ------------------------------------------------------------------

def compute_weekly_exercise_stats(user_id: str, sets_df: pd.DataFrame) -> pd.DataFrame:
    if sets_df.empty:
        return pd.DataFrame(columns=EXERCISE_STATS_COLUMNS)

    working = sets_df[~sets_df["is_warmup"]].copy()
    if working.empty:
        return pd.DataFrame(columns=EXERCISE_STATS_COLUMNS)

    working["week"] = working["workout_date"].apply(week_start)
    working["set_volume_kg"] = working.apply(
        lambda r: r["weight_kg"] * r["reps"]
        if pd.notna(r["weight_kg"]) and pd.notna(r["reps"]) else 0.0,
        axis=1,
    )
    working["set_est_1rm"] = working.apply(
        lambda r: estimate_1rm(r["weight_kg"], r["reps"]), axis=1
    )

    rows = []
    for (week, template_id), group in working.groupby(["week", "exercise_template_id"]):
        est_1rms = group["set_est_1rm"].dropna()
        max_weights = group["weight_kg"].dropna()
        reps_values = group["reps"].dropna()
        rows.append({
            "user_id": user_id,
            "week": week,
            "exercise_template_id": template_id,
            "exercise_title": group["exercise_title"].iloc[0],
            "total_volume_kg": group["set_volume_kg"].sum(),
            "max_weight_kg": max_weights.max() if not max_weights.empty else None,
            "mean_reps": reps_values.mean() if not reps_values.empty else None,
            "best_est_1rm": est_1rms.max() if not est_1rms.empty else None,
            "set_count": len(group),
            "total_distance_meters": group["distance_meters"].dropna().sum() or None,
            "total_duration_seconds": group["duration_seconds"].dropna().sum() or None,
            "schema_version": SCHEMA_VERSION,
        })
    return pd.DataFrame(rows, columns=EXERCISE_STATS_COLUMNS)


def compute_weekly_summary(user_id: str, sets_df: pd.DataFrame) -> pd.DataFrame:
    if sets_df.empty:
        return pd.DataFrame(columns=SUMMARY_COLUMNS)

    working = sets_df.copy()
    working["week"] = working["workout_date"].apply(week_start)
    non_warmup = working[~working["is_warmup"]].copy()
    non_warmup["set_volume_kg"] = non_warmup.apply(
        lambda r: r["weight_kg"] * r["reps"]
        if pd.notna(r["weight_kg"]) and pd.notna(r["reps"]) else 0.0,
        axis=1,
    )

    rows = []
    for week, group in working.groupby("week"):
        volume_group = non_warmup[non_warmup["week"] == week]
        rows.append({
            "user_id": user_id,
            "week": week,
            "total_volume_kg": volume_group["set_volume_kg"].sum(),
            "workout_count": group["workout_id"].nunique(),
            "total_sets": len(group),
            "schema_version": SCHEMA_VERSION,
        })
    return pd.DataFrame(rows, columns=SUMMARY_COLUMNS)


# ---- Gold writer (overwrite per partition — gold is a derived, recomputed cache) ----

def build_gold_key(dataset: str, user_id: str, week: str) -> str:
    return f"{dataset}/user_id={user_id}/week={week}/data.parquet"


def write_gold_partitions(s3_client, bucket: str, dataset: str, user_id: str,
                           df: pd.DataFrame, columns: list[str], week_col: str = "week") -> int:
    written = 0
    for week, group in df.groupby(week_col):
        buffer = io.BytesIO()
        group[columns].to_parquet(buffer, engine="pyarrow", index=False)
        key = build_gold_key(dataset, user_id, week)
        s3_client.put_object(Bucket=bucket, Key=key, Body=buffer.getvalue())
        written += 1
    return written


# ---- Entrypoint ------------------------------------------------------------------

def run(user_id: str) -> dict:
    silver_bucket = os.environ["SILVER_BUCKET"]
    gold_bucket = os.environ["GOLD_BUCKET"]

    s3_client = boto3.client("s3")

    sets_df = load_all(s3_client, silver_bucket, "sets", user_id, [])
    deleted_df = load_all(s3_client, silver_bucket, "deleted_workouts", user_id, [])

    sets_df = dedup_sets(sets_df)
    sets_df = exclude_deleted_workouts(sets_df, deleted_df)

    exercise_stats = compute_weekly_exercise_stats(user_id, sets_df)
    summary = compute_weekly_summary(user_id, sets_df)

    weeks_written = write_gold_partitions(
        s3_client, gold_bucket, "weekly_exercise_stats", user_id, exercise_stats, EXERCISE_STATS_COLUMNS
    )
    write_gold_partitions(
        s3_client, gold_bucket, "weekly_summary", user_id, summary, SUMMARY_COLUMNS
    )

    return {
        "user_id": user_id,
        "sets_considered": len(sets_df),
        "exercise_stat_rows": len(exercise_stats),
        "summary_rows": len(summary),
        "weeks_written": weeks_written,
    }


def apply_cli_args_as_env(argv: list[str]) -> None:
    """Same shim as B2 — Glue Python Shell passes job parameters as
    --KEY value CLI args, not OS env vars."""
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
