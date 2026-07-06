from decimal import Decimal

import boto3
import pytest
from moto import mock_aws

from tools.query_workout_history import clamp_weeks, query_workout_history

TABLE_NAME = "stats-test"


# ---- clamp_weeks ------------------------------------------------------------

def test_clamp_weeks_within_range_unchanged():
    assert clamp_weeks(8) == 8


def test_clamp_weeks_floors_below_one():
    assert clamp_weeks(0) == 1
    assert clamp_weeks(-5) == 1


def test_clamp_weeks_caps_at_max():
    assert clamp_weeks(999) == 52


# ---- query_workout_history (moto DynamoDB) -----------------------------------

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
def aws_env(monkeypatch):
    monkeypatch.setenv("STATS_TABLE_NAME", TABLE_NAME)
    monkeypatch.setenv("TARGET_USER_ID", "demo-user")
    with mock_aws():
        table = _make_table(boto3.resource("dynamodb", region_name="eu-west-1"))
        yield table


def test_query_workout_history_empty_when_no_data(aws_env):
    result = query_workout_history()
    assert result == {"weeks": []}


def test_query_workout_history_returns_oldest_to_newest(aws_env):
    for week in ["2026-06-01", "2026-06-15", "2026-06-08"]:
        aws_env.put_item(Item={
            "user_id": "demo-user", "stat_type": f"WEEK#{week}", "week": week,
            "total_volume_kg": Decimal("500"),
        })

    result = query_workout_history(weeks=8)

    assert [w["week"] for w in result["weeks"]] == ["2026-06-01", "2026-06-08", "2026-06-15"]


def test_query_workout_history_respects_weeks_limit(aws_env):
    for week in ["2026-06-01", "2026-06-08", "2026-06-15", "2026-06-22"]:
        aws_env.put_item(Item={"user_id": "demo-user", "stat_type": f"WEEK#{week}", "week": week})

    result = query_workout_history(weeks=2)

    assert [w["week"] for w in result["weeks"]] == ["2026-06-15", "2026-06-22"]


def test_query_workout_history_excludes_latest_item(aws_env):
    aws_env.put_item(Item={"user_id": "demo-user", "stat_type": "LATEST", "week": "2026-06-22"})
    aws_env.put_item(Item={"user_id": "demo-user", "stat_type": "WEEK#2026-06-22", "week": "2026-06-22"})

    result = query_workout_history()

    assert len(result["weeks"]) == 1


def test_query_workout_history_only_reads_target_user(aws_env):
    aws_env.put_item(Item={"user_id": "someone-else", "stat_type": "WEEK#2026-06-01", "week": "2026-06-01"})
    result = query_workout_history()
    assert result == {"weeks": []}
