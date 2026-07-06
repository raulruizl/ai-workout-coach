import io
import math
from decimal import Decimal

import boto3
import pandas as pd
import pytest
from moto import mock_aws

import handler as h

GOLD_BUCKET = "gold-test"
TABLE_NAME = "stats-test"


# ---- to_number -----------------------------------------------------------------

def test_to_number_converts_float_to_decimal():
    assert h.to_number(12.5) == Decimal("12.5")


def test_to_number_none_stays_none():
    assert h.to_number(None) is None


def test_to_number_nan_becomes_none():
    assert h.to_number(float("nan")) is None


# ---- item builders ---------------------------------------------------------------

def _summary_row(week="2026-06-29", total_volume_kg=1000.0, workout_count=3, total_sets=40):
    return pd.Series({"week": week, "total_volume_kg": total_volume_kg,
                       "workout_count": workout_count, "total_sets": total_sets})


def _exercise_df(week="2026-06-29"):
    return pd.DataFrame([
        {"week": week, "exercise_template_id": "squat", "exercise_title": "Squat",
         "total_volume_kg": 500.0, "max_weight_kg": 100.0, "mean_reps": 5.0,
         "best_est_1rm": math.nan, "set_count": 3},
    ])


def test_build_exercise_items_handles_nan_1rm():
    items = h.build_exercise_items(_exercise_df())
    assert items[0]["best_est_1rm"] is None
    assert items[0]["max_weight_kg"] == Decimal("100.0")
    assert items[0]["set_count"] == 3


def test_build_week_item_shape():
    item = h.build_week_item("demo-user", "2026-06-29", "WEEK#2026-06-29", _summary_row(), _exercise_df())
    assert item["user_id"] == "demo-user"
    assert item["stat_type"] == "WEEK#2026-06-29"
    assert item["workout_count"] == 3
    assert len(item["exercises"]) == 1


def test_build_all_items_adds_latest_pointing_at_most_recent_week():
    summary_df = pd.DataFrame([_summary_row("2026-06-22", 800.0, 2, 30), _summary_row("2026-06-29", 1000.0, 3, 40)])
    exercise_df = pd.concat([_exercise_df("2026-06-22"), _exercise_df("2026-06-29")], ignore_index=True)

    items = h.build_all_items("demo-user", summary_df, exercise_df)

    stat_types = {item["stat_type"] for item in items}
    assert stat_types == {"WEEK#2026-06-22", "WEEK#2026-06-29", "LATEST"}

    latest = next(i for i in items if i["stat_type"] == "LATEST")
    assert latest["week"] == "2026-06-29"
    assert latest["workout_count"] == 3


def test_build_all_items_empty_summary_returns_nothing():
    assert h.build_all_items("demo-user", pd.DataFrame(), pd.DataFrame()) == []


# ---- gold readers (moto S3) --------------------------------------------------------

def _put_gold_parquet(s3_client, dataset: str, user_id: str, week: str, rows: list[dict]):
    df = pd.DataFrame(rows)
    buffer = io.BytesIO()
    df.to_parquet(buffer, engine="pyarrow", index=False)
    key = f"{dataset}/user_id={user_id}/week={week}/data.parquet"
    s3_client.put_object(Bucket=GOLD_BUCKET, Key=key, Body=buffer.getvalue())


@mock_aws
def test_load_gold_dataset_concatenates_all_weeks():
    s3_client = boto3.client("s3", region_name="eu-west-1")
    s3_client.create_bucket(Bucket=GOLD_BUCKET, CreateBucketConfiguration={"LocationConstraint": "eu-west-1"})
    _put_gold_parquet(s3_client, "weekly_summary", "demo-user", "2026-06-22", [_summary_row("2026-06-22").to_dict()])
    _put_gold_parquet(s3_client, "weekly_summary", "demo-user", "2026-06-29", [_summary_row("2026-06-29").to_dict()])

    df = h.load_gold_dataset(s3_client, GOLD_BUCKET, "weekly_summary", "demo-user")
    assert len(df) == 2
    assert set(df["week"]) == {"2026-06-22", "2026-06-29"}


@mock_aws
def test_load_gold_dataset_empty_when_no_keys():
    s3_client = boto3.client("s3", region_name="eu-west-1")
    s3_client.create_bucket(Bucket=GOLD_BUCKET, CreateBucketConfiguration={"LocationConstraint": "eu-west-1"})
    df = h.load_gold_dataset(s3_client, GOLD_BUCKET, "weekly_summary", "demo-user")
    assert df.empty


# ---- full sync() integration (moto S3 + DynamoDB) -----------------------------------

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


@pytest.fixture
def aws_env():
    with mock_aws():
        import os
        os.environ["GOLD_BUCKET"] = GOLD_BUCKET
        os.environ["STATS_TABLE_NAME"] = TABLE_NAME

        s3_client = boto3.client("s3", region_name="eu-west-1")
        s3_client.create_bucket(Bucket=GOLD_BUCKET, CreateBucketConfiguration={"LocationConstraint": "eu-west-1"})
        table = _make_table(boto3.resource("dynamodb"))

        yield {"s3": s3_client, "table": table}


def test_sync_writes_week_and_latest_items(aws_env):
    _put_gold_parquet(aws_env["s3"], "weekly_summary", "demo-user", "2026-06-29",
                       [_summary_row("2026-06-29").to_dict()])
    _put_gold_parquet(aws_env["s3"], "weekly_exercise_stats", "demo-user", "2026-06-29",
                       _exercise_df("2026-06-29").to_dict("records"))

    result = h.sync("demo-user")

    assert result["weeks_found"] == 1
    assert result["items_written"] == 2  # WEEK#... + LATEST

    latest = aws_env["table"].get_item(Key={"user_id": "demo-user", "stat_type": "LATEST"})["Item"]
    assert latest["week"] == "2026-06-29"
    assert latest["exercises"][0]["exercise_template_id"] == "squat"
    assert latest["exercises"][0]["best_est_1rm"] is None

    week_item = aws_env["table"].get_item(Key={"user_id": "demo-user", "stat_type": "WEEK#2026-06-29"})["Item"]
    assert week_item["total_sets"] == 40


def test_sync_no_gold_data_writes_nothing(aws_env):
    result = h.sync("demo-user")
    assert result == {"user_id": "demo-user", "weeks_found": 0, "items_written": 0}
