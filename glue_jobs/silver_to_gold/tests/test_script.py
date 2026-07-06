import io

import boto3
import pandas as pd
import pytest
from moto import mock_aws

import script as s

SILVER_BUCKET = "silver-test"
GOLD_BUCKET = "gold-test"

SET_COLUMNS = [
    "user_id", "workout_id", "workout_date", "exercise_template_id", "exercise_title",
    "set_index", "set_type", "is_warmup", "weight_kg", "reps", "distance_meters",
    "duration_seconds", "rpe", "schema_version", "_ingested_at",
]
DELETED_COLUMNS = ["user_id", "workout_id", "deleted_at", "schema_version", "_ingested_at"]


def _set_row(**overrides):
    row = {
        "user_id": "demo-user", "workout_id": "wk_1", "workout_date": "2026-07-01",
        "exercise_template_id": "squat", "exercise_title": "Squat", "set_index": 0,
        "set_type": "normal", "is_warmup": False, "weight_kg": 100.0, "reps": 5,
        "distance_meters": None, "duration_seconds": None, "rpe": 8,
        "schema_version": 1, "_ingested_at": "2026-07-01T11:00:00+00:00",
    }
    row.update(overrides)
    return row


# ---- week_start ----------------------------------------------------------------

def test_week_start_returns_monday():
    assert s.week_start("2026-07-01") == "2026-06-29"  # Wed -> preceding Monday
    assert s.week_start("2026-06-29") == "2026-06-29"  # Monday -> itself


# ---- estimate_1rm ----------------------------------------------------------------

def test_estimate_1rm_epley_formula():
    assert s.estimate_1rm(100.0, 5) == pytest.approx(100 * (1 + 5 / 30))


def test_estimate_1rm_null_above_12_reps():
    assert s.estimate_1rm(50.0, 13) is None


def test_estimate_1rm_null_when_weight_or_reps_missing():
    assert s.estimate_1rm(None, 5) is None
    assert s.estimate_1rm(100.0, None) is None


# ---- dedup -----------------------------------------------------------------------

def test_dedup_sets_keeps_latest_ingested_version():
    df = pd.DataFrame([
        _set_row(weight_kg=90.0, _ingested_at="2026-07-01T10:00:00+00:00"),
        _set_row(weight_kg=100.0, _ingested_at="2026-07-01T12:00:00+00:00"),
    ])
    result = s.dedup_sets(df)
    assert len(result) == 1
    assert result.iloc[0]["weight_kg"] == 100.0


def test_dedup_sets_distinguishes_by_composite_key():
    df = pd.DataFrame([
        _set_row(set_index=0),
        _set_row(set_index=1),
        _set_row(exercise_template_id="bench"),
    ])
    result = s.dedup_sets(df)
    assert len(result) == 3


# ---- deleted workout exclusion -----------------------------------------------------

def test_exclude_deleted_workouts_drops_matching_workout_id():
    sets_df = pd.DataFrame([_set_row(workout_id="wk_1"), _set_row(workout_id="wk_2")])
    deleted_df = pd.DataFrame([{"user_id": "demo-user", "workout_id": "wk_1",
                                 "deleted_at": "2026-07-02T00:00:00Z", "schema_version": 1,
                                 "_ingested_at": "x"}])
    result = s.exclude_deleted_workouts(sets_df, deleted_df)
    assert list(result["workout_id"]) == ["wk_2"]


def test_exclude_deleted_workouts_noop_when_nothing_deleted():
    sets_df = pd.DataFrame([_set_row()])
    result = s.exclude_deleted_workouts(sets_df, pd.DataFrame(columns=DELETED_COLUMNS))
    assert len(result) == 1


# ---- weekly exercise stats: warmup exclusion + null time/distance handling --------

def test_weekly_exercise_stats_excludes_warmup_from_volume():
    df = pd.DataFrame([
        _set_row(set_index=0, is_warmup=True, weight_kg=40.0, reps=10),
        _set_row(set_index=1, is_warmup=False, weight_kg=100.0, reps=5),
    ])
    result = s.compute_weekly_exercise_stats("demo-user", df)
    assert len(result) == 1  # warmup row contributes no separate group, only reduces volume
    row = result.iloc[0]
    assert row["total_volume_kg"] == 500.0  # only the non-warmup set
    assert row["max_weight_kg"] == 100.0  # warmup's 40kg excluded
    assert row["mean_reps"] == 5.0
    assert row["set_count"] == 1


def test_weekly_exercise_stats_handles_time_distance_exercise_without_crashing():
    df = pd.DataFrame([
        _set_row(exercise_template_id="run", exercise_title="Run", weight_kg=None, reps=None,
                 distance_meters=5000.0, duration_seconds=1800),
    ])
    result = s.compute_weekly_exercise_stats("demo-user", df)
    assert len(result) == 1
    row = result.iloc[0]
    assert row["total_volume_kg"] == 0.0
    assert row["max_weight_kg"] is None
    assert row["mean_reps"] is None
    assert row["best_est_1rm"] is None
    assert row["total_distance_meters"] == 5000.0
    assert row["total_duration_seconds"] == 1800


def test_weekly_exercise_stats_best_est_1rm_ignores_reps_over_12():
    df = pd.DataFrame([
        _set_row(set_index=0, weight_kg=100.0, reps=5),
        _set_row(set_index=1, weight_kg=60.0, reps=15),
    ])
    result = s.compute_weekly_exercise_stats("demo-user", df)
    row = result.iloc[0]
    assert row["best_est_1rm"] == pytest.approx(100 * (1 + 5 / 30))


def test_weekly_exercise_stats_empty_input():
    result = s.compute_weekly_exercise_stats("demo-user", pd.DataFrame(columns=SET_COLUMNS))
    assert result.empty


# ---- weekly summary ---------------------------------------------------------------

def test_weekly_summary_counts_workouts_and_sets_including_warmup():
    df = pd.DataFrame([
        _set_row(workout_id="wk_1", set_index=0, is_warmup=True, weight_kg=40.0, reps=10),
        _set_row(workout_id="wk_1", set_index=1, is_warmup=False, weight_kg=100.0, reps=5),
        _set_row(workout_id="wk_2", set_index=0, is_warmup=False, weight_kg=50.0, reps=8),
    ])
    result = s.compute_weekly_summary("demo-user", df)
    assert len(result) == 1
    row = result.iloc[0]
    assert row["workout_count"] == 2
    assert row["total_sets"] == 3
    assert row["total_volume_kg"] == 500.0 + 400.0  # warmup excluded from volume


# ---- gold writer (moto S3) ---------------------------------------------------------

@mock_aws
def test_write_gold_partitions_splits_by_week():
    s3_client = boto3.client("s3", region_name="eu-west-1")
    s3_client.create_bucket(Bucket=GOLD_BUCKET, CreateBucketConfiguration={"LocationConstraint": "eu-west-1"})

    df = pd.DataFrame([
        {"user_id": "demo-user", "week": "2026-06-29", "total_volume_kg": 500.0,
         "workout_count": 1, "total_sets": 1, "schema_version": 1},
        {"user_id": "demo-user", "week": "2026-07-06", "total_volume_kg": 300.0,
         "workout_count": 1, "total_sets": 1, "schema_version": 1},
    ])

    written = s.write_gold_partitions(s3_client, GOLD_BUCKET, "weekly_summary", "demo-user", df, s.SUMMARY_COLUMNS)
    assert written == 2

    listing = s3_client.list_objects_v2(Bucket=GOLD_BUCKET, Prefix="weekly_summary/user_id=demo-user/")
    assert listing["KeyCount"] == 2

    obj = s3_client.get_object(Bucket=GOLD_BUCKET, Key="weekly_summary/user_id=demo-user/week=2026-06-29/data.parquet")
    read_back = pd.read_parquet(io.BytesIO(obj["Body"].read()))
    assert read_back.iloc[0]["total_volume_kg"] == 500.0


# ---- full run() integration ---------------------------------------------------------

def _put_silver(s3_client, dataset: str, user_id: str, year_month: str, run_id: str, rows: list[dict], columns: list[str]):
    df = pd.DataFrame(rows, columns=columns)
    buffer = io.BytesIO()
    df.to_parquet(buffer, engine="pyarrow", index=False)
    key = f"{dataset}/user_id={user_id}/year_month={year_month}/run_{run_id}.parquet"
    s3_client.put_object(Bucket=SILVER_BUCKET, Key=key, Body=buffer.getvalue())


@pytest.fixture
def aws_env():
    with mock_aws():
        import os
        os.environ["SILVER_BUCKET"] = SILVER_BUCKET
        os.environ["GOLD_BUCKET"] = GOLD_BUCKET

        s3_client = boto3.client("s3", region_name="eu-west-1")
        s3_client.create_bucket(Bucket=SILVER_BUCKET, CreateBucketConfiguration={"LocationConstraint": "eu-west-1"})
        s3_client.create_bucket(Bucket=GOLD_BUCKET, CreateBucketConfiguration={"LocationConstraint": "eu-west-1"})

        yield {"s3": s3_client}


def test_run_computes_gold_from_silver_and_excludes_deleted(aws_env):
    s3_client = aws_env["s3"]
    _put_silver(s3_client, "sets", "demo-user", "2026-07", "run1", [
        _set_row(workout_id="wk_1", set_index=0, is_warmup=True, weight_kg=40.0, reps=10),
        _set_row(workout_id="wk_1", set_index=1, is_warmup=False, weight_kg=100.0, reps=5),
        _set_row(workout_id="wk_2", set_index=0, is_warmup=False, weight_kg=50.0, reps=8),
    ], SET_COLUMNS)
    _put_silver(s3_client, "deleted_workouts", "demo-user", "2026-07", "run1", [
        {"user_id": "demo-user", "workout_id": "wk_2", "deleted_at": "2026-07-02T00:00:00Z",
         "schema_version": 1, "_ingested_at": "2026-07-02T00:00:00+00:00"},
    ], DELETED_COLUMNS)

    result = s.run("demo-user")

    assert result["sets_considered"] == 2  # wk_2's set excluded (deleted)
    assert result["weeks_written"] == 1

    listing = s3_client.list_objects_v2(Bucket=GOLD_BUCKET, Prefix="weekly_exercise_stats/user_id=demo-user/")
    assert listing["KeyCount"] == 1

    obj = s3_client.get_object(Bucket=GOLD_BUCKET, Key="weekly_exercise_stats/user_id=demo-user/week=2026-06-29/data.parquet")
    df = pd.read_parquet(io.BytesIO(obj["Body"].read()))
    assert df.iloc[0]["total_volume_kg"] == 500.0  # only wk_1's non-warmup set
