from decimal import Decimal

import boto3
import pytest
from moto import mock_aws

import tools.propose_progression as pp
from tools.propose_progression import compute_proposed_weight, find_latest_exercise_entry, propose_progression

TABLE_NAME = "stats-test"

HISTORY = {
    "weeks": [
        {"week": "2026-06-01", "exercises": [
            {"exercise_template_id": "3601968B", "exercise_title": "Bench Press", "max_weight_kg": 18.0, "mean_reps": 8},
        ]},
        {"week": "2026-06-08", "exercises": [
            {"exercise_template_id": "3601968B", "exercise_title": "Bench Press", "max_weight_kg": 20.0, "mean_reps": 9},
            {"exercise_template_id": "F1D60854", "exercise_title": "Cable Row", "max_weight_kg": 45.0, "mean_reps": 10},
        ]},
    ]
}


# ---- compute_proposed_weight -------------------------------------------------

def test_compute_proposed_weight_adds_fixed_increment():
    assert compute_proposed_weight(20.0) == 22.5


# ---- find_latest_exercise_entry ----------------------------------------------

def test_find_latest_exercise_entry_returns_most_recent_week():
    entry = find_latest_exercise_entry(HISTORY, "3601968B")
    assert entry["max_weight_kg"] == 20.0  # 2026-06-08, not the earlier week


def test_find_latest_exercise_entry_none_when_not_present():
    assert find_latest_exercise_entry(HISTORY, "unknown-id") is None


def test_find_latest_exercise_entry_empty_history():
    assert find_latest_exercise_entry({"weeks": []}, "3601968B") is None


# ---- propose_progression (moto DynamoDB) --------------------------------------

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
    monkeypatch.setattr(pp, "fetch_history", lambda weeks=1: HISTORY)
    with mock_aws():
        table = _make_table(boto3.resource("dynamodb", region_name="eu-west-1"))
        yield table


def test_propose_progression_returns_expected_shape(aws_env):
    result = propose_progression("3601968B")

    assert result["exercise_title"] == "Bench Press"
    assert result["current_weight_kg"] == 20.0
    assert result["proposed_weight_kg"] == 22.5
    assert result["reps"] == 9
    assert "proposal_id" in result


def test_propose_progression_persists_pending_item(aws_env):
    result = propose_progression("3601968B")

    item = aws_env.get_item(Key={"user_id": "demo-user", "stat_type": f"PROPOSAL#{result['proposal_id']}"})["Item"]
    assert item["status"] == "pending"
    assert item["proposed_weight_kg"] == Decimal("22.5")
    assert item["exercise_template_id"] == "3601968B"
    assert "ttl" in item


def test_propose_progression_error_when_no_history(aws_env):
    result = propose_progression("does-not-exist")
    assert "error" in result


def test_propose_progression_accepts_no_weight_or_reps_params():
    """The model must not be able to supply numbers — the tool signature
    itself is the gate (removed chat-era weight_kg/reps params)."""
    with pytest.raises(TypeError):
        propose_progression("3601968B", weight_kg=60.0)  # noqa — intentional bad call
    with pytest.raises(TypeError):
        propose_progression("3601968B", reps=8)  # noqa — intentional bad call
