import os
from decimal import Decimal

import boto3
import pytest
from moto import mock_aws

from tools.get_latest_stats import get_latest_stats, to_plain

TABLE_NAME = "stats-test"


# ---- to_plain --------------------------------------------------------------

def test_to_plain_converts_whole_decimal_to_int():
    assert to_plain(Decimal("5")) == 5
    assert isinstance(to_plain(Decimal("5")), int)


def test_to_plain_converts_fractional_decimal_to_float():
    assert to_plain(Decimal("5.5")) == 5.5
    assert isinstance(to_plain(Decimal("5.5")), float)


def test_to_plain_recurses_into_nested_structures():
    result = to_plain({"a": [Decimal("1"), {"b": Decimal("2.5")}]})
    assert result == {"a": [1, {"b": 2.5}]}


# ---- get_latest_stats (moto DynamoDB) ---------------------------------------

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


def test_get_latest_stats_returns_error_when_no_data(aws_env):
    result = get_latest_stats()
    assert "error" in result


def test_get_latest_stats_returns_item_without_key_fields(aws_env):
    aws_env.put_item(Item={
        "user_id": "demo-user", "stat_type": "LATEST", "week": "2026-06-29",
        "total_volume_kg": Decimal("500.0"), "workout_count": 3, "total_sets": Decimal("40"),
        "exercises": [{"exercise_title": "Squat", "best_est_1rm": Decimal("120.5")}],
    })

    result = get_latest_stats()

    assert "user_id" not in result
    assert "stat_type" not in result
    assert result["week"] == "2026-06-29"
    assert result["total_sets"] == 40
    assert isinstance(result["total_sets"], int)
    assert result["exercises"][0]["best_est_1rm"] == 120.5


def test_get_latest_stats_only_reads_target_user(aws_env):
    aws_env.put_item(Item={"user_id": "someone-else", "stat_type": "LATEST", "week": "2020-01-01"})
    result = get_latest_stats()
    assert "error" in result
