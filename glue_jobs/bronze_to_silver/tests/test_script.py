import gzip
import io
import json
import os

import boto3
import pandas as pd
import pytest
from moto import mock_aws

import script as s

BRONZE_BUCKET = "bronze-test"
SILVER_BUCKET = "silver-test"
TABLE_NAME = "stats-test"

STRENGTH_WORKOUT = {
    "id": "wk_1",
    "start_time": "2026-07-01T10:00:00Z",
    "exercises": [
        {
            "exercise_template_id": "squat",
            "title": "Squat",
            "sets": [
                {"index": 0, "type": "warmup", "weight_kg": 40, "reps": 10, "rpe": None},
                {"index": 1, "type": "normal", "weight_kg": 100, "reps": 5, "rpe": 8},
            ],
        }
    ],
}

CARDIO_WORKOUT = {
    "id": "wk_2",
    "start_time": "2026-08-15T08:00:00Z",
    "exercises": [
        {
            "exercise_template_id": "run",
            "title": "Treadmill Run",
            "sets": [
                {"index": 0, "type": "normal", "weight_kg": None, "reps": None,
                 "distance_meters": 5000, "duration_seconds": 1800, "rpe": 6},
            ],
        }
    ],
}


# ---- key parsing / cursor filtering ------------------------------------------

def test_run_id_from_key_strips_prefix_and_suffix():
    key = "workouts/user_id=demo/ingest_date=2026-07-05/run_20260705T183524786592.json.gz"
    assert s._run_id_from_key(key) == "20260705T183524786592"


def test_unprocessed_keys_filters_by_cursor():
    keys = [
        "workouts/user_id=demo/ingest_date=2026-07-01/run_20260701T000000000000.json.gz",
        "workouts/user_id=demo/ingest_date=2026-07-05/run_20260705T000000000000.json.gz",
    ]
    result = s.unprocessed_keys(keys, "20260701T000000000000")
    assert result == [keys[1]]


def test_unprocessed_keys_empty_cursor_returns_all():
    keys = ["workouts/user_id=demo/ingest_date=2026-07-01/run_20260701T000000000000.json.gz"]
    assert s.unprocessed_keys(keys, "") == keys


# ---- flatten ------------------------------------------------------------------

def test_flatten_strength_workout_flags_warmup():
    rows = s.flatten_workout_to_set_rows("demo-user", STRENGTH_WORKOUT, "2026-07-01T11:00:00+00:00")
    assert len(rows) == 2
    assert rows[0]["is_warmup"] is True
    assert rows[0]["set_type"] == "warmup"
    assert rows[1]["is_warmup"] is False
    assert rows[1]["weight_kg"] == 100
    assert rows[0]["workout_date"] == "2026-07-01"
    assert rows[0]["workout_id"] == "wk_1"


def test_flatten_cardio_workout_has_null_weight_and_reps():
    rows = s.flatten_workout_to_set_rows("demo-user", CARDIO_WORKOUT, "2026-08-15T09:00:00+00:00")
    assert len(rows) == 1
    assert rows[0]["weight_kg"] is None
    assert rows[0]["reps"] is None
    assert rows[0]["distance_meters"] == 5000
    assert rows[0]["duration_seconds"] == 1800


def test_process_events_splits_updated_and_deleted():
    events = [
        {"type": "updated", "workout": STRENGTH_WORKOUT},
        {"type": "deleted", "id": "wk_old", "deleted_at": "2026-07-02T00:00:00Z"},
    ]
    set_rows, deleted_rows = s.process_events("demo-user", events, "2026-07-01T11:00:00+00:00")
    assert len(set_rows) == 2
    assert len(deleted_rows) == 1
    assert deleted_rows[0]["workout_id"] == "wk_old"
    assert deleted_rows[0]["user_id"] == "demo-user"


def test_group_by_year_month_splits_across_months():
    rows = (
        s.flatten_workout_to_set_rows("demo-user", STRENGTH_WORKOUT, "x")
        + s.flatten_workout_to_set_rows("demo-user", CARDIO_WORKOUT, "x")
    )
    groups = s.group_by_year_month(rows, "workout_date")
    assert set(groups.keys()) == {"2026-07", "2026-08"}
    assert len(groups["2026-07"]) == 2
    assert len(groups["2026-08"]) == 1


# ---- CLI args shim (Glue Python Shell job parameters) --------------------------

def test_apply_cli_args_as_env_sets_new_vars(monkeypatch):
    monkeypatch.delenv("SOME_GLUE_ARG", raising=False)
    s.apply_cli_args_as_env(["--SOME_GLUE_ARG", "value1", "--OTHER", "value2"])
    assert os.environ["SOME_GLUE_ARG"] == "value1"
    assert os.environ["OTHER"] == "value2"


def test_apply_cli_args_as_env_does_not_override_existing(monkeypatch):
    monkeypatch.setenv("SOME_GLUE_ARG", "already-set")
    s.apply_cli_args_as_env(["--SOME_GLUE_ARG", "new-value"])
    assert os.environ["SOME_GLUE_ARG"] == "already-set"


# ---- silver writer (moto S3) --------------------------------------------------

@mock_aws
def test_write_parquet_roundtrips():
    s3_client = boto3.client("s3", region_name="eu-west-1")
    s3_client.create_bucket(Bucket=SILVER_BUCKET, CreateBucketConfiguration={"LocationConstraint": "eu-west-1"})
    rows = s.flatten_workout_to_set_rows("demo-user", STRENGTH_WORKOUT, "2026-07-01T11:00:00+00:00")

    key = s.build_silver_key("sets", "demo-user", "2026-07", "run1")
    s.write_parquet(s3_client, SILVER_BUCKET, key, rows, s.SET_COLUMNS)

    obj = s3_client.get_object(Bucket=SILVER_BUCKET, Key=key)
    df = pd.read_parquet(io.BytesIO(obj["Body"].read()))
    assert len(df) == 2
    assert list(df.columns) == s.SET_COLUMNS
    assert df.iloc[0]["is_warmup"] == True  # noqa: E712 (parquet round-trip bool check)


# ---- cursor (moto DynamoDB) ---------------------------------------------------

def _make_table(dynamodb):
    dynamodb.create_table(
        TableName=TABLE_NAME,
        KeySchema=[
            {"AttributeName": "user_id", "KeyType": "HASH"},
            {"AttributeName": "stat_type", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "user_id", "AttributeType": "S"},
            {"AttributeName": "stat_type", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    return dynamodb.Table(TABLE_NAME)


@mock_aws
def test_cursor_defaults_empty_then_roundtrips():
    table = _make_table(boto3.resource("dynamodb"))
    assert s.get_cursor(table, "demo-user") == ""
    s.set_cursor(table, "demo-user", "20260705T120000000000")
    assert s.get_cursor(table, "demo-user") == "20260705T120000000000"


# ---- full run() integration ----------------------------------------------------

def _put_bronze(s3_client, run_id: str, events: list[dict]):
    payload = {"schema_version": 1, "_ingested_at": f"2026-07-05T{run_id[9:11]}:00:00+00:00",
               "user_id": "demo-user", "events": events}
    body = gzip.compress(json.dumps(payload).encode("utf-8"))
    key = f"workouts/user_id=demo-user/ingest_date=2026-07-05/run_{run_id}.json.gz"
    s3_client.put_object(Bucket=BRONZE_BUCKET, Key=key, Body=body)


@pytest.fixture
def aws_env():
    with mock_aws():
        os.environ["BRONZE_BUCKET"] = BRONZE_BUCKET
        os.environ["SILVER_BUCKET"] = SILVER_BUCKET
        os.environ["STATS_TABLE_NAME"] = TABLE_NAME

        s3_client = boto3.client("s3", region_name="eu-west-1")
        s3_client.create_bucket(Bucket=BRONZE_BUCKET, CreateBucketConfiguration={"LocationConstraint": "eu-west-1"})
        s3_client.create_bucket(Bucket=SILVER_BUCKET, CreateBucketConfiguration={"LocationConstraint": "eu-west-1"})
        table = _make_table(boto3.resource("dynamodb"))

        yield {"s3": s3_client, "table": table}


def test_run_processes_new_bronze_and_writes_silver(aws_env):
    _put_bronze(aws_env["s3"], "20260705T100000000000", [{"type": "updated", "workout": STRENGTH_WORKOUT}])

    result = s.run("demo-user")

    assert result["bronze_objects_processed"] == 1
    assert result["set_rows_written"] == 2
    assert result["deleted_rows_written"] == 0

    listing = aws_env["s3"].list_objects_v2(Bucket=SILVER_BUCKET, Prefix="sets/user_id=demo-user/")
    assert listing["KeyCount"] == 1


def test_run_is_incremental_across_two_runs(aws_env):
    _put_bronze(aws_env["s3"], "20260705T100000000000", [{"type": "updated", "workout": STRENGTH_WORKOUT}])
    s.run("demo-user")

    _put_bronze(aws_env["s3"], "20260705T110000000000", [{"type": "deleted", "id": "wk_old", "deleted_at": "2026-07-05T11:00:00Z"}])
    second_result = s.run("demo-user")

    assert second_result["bronze_objects_processed"] == 1  # only the new one
    assert second_result["set_rows_written"] == 0
    assert second_result["deleted_rows_written"] == 1


def test_run_advances_cursor_in_dynamodb(aws_env):
    _put_bronze(aws_env["s3"], "20260705T100000000000", [{"type": "updated", "workout": STRENGTH_WORKOUT}])
    s.run("demo-user")

    cursor_item = aws_env["table"].get_item(Key={"user_id": "demo-user", "stat_type": s.SILVER_CURSOR_SK})["Item"]
    assert cursor_item["run_id"] == "20260705T100000000000"
