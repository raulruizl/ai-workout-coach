import time
from decimal import Decimal

import boto3
import pytest
from moto import mock_aws

import tools.apply_progression as ap
from tools.apply_progression import apply_progression, claim_proposal
from tools.hevy_client import HevyAPIError

TABLE_NAME = "stats-test"

FAKE_ROUTINE = {
    "id": "routine-1",
    "title": "Push",
    "exercises": [{
        "exercise_template_id": "3601968B",
        "sets": [{"type": "normal", "weight_kg": 20.0, "reps": 9}],
    }],
}


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
    monkeypatch.setenv("HEVY_API_KEY_PARAM", "/workout-coach/hevy-api-key")
    with mock_aws():
        table = _make_table(boto3.resource("dynamodb", region_name="eu-west-1"))
        yield table


def _put_pending(table, proposal_id="abc123", ttl_offset=600, **overrides):
    item = {
        "user_id": "demo-user", "stat_type": f"PROPOSAL#{proposal_id}",
        "exercise_template_id": "3601968B", "exercise_title": "Bench Press",
        "current_weight_kg": Decimal("20.0"), "proposed_weight_kg": Decimal("22.5"),
        "reps": 9, "status": "pending", "ttl": int(time.time()) + ttl_offset,
    }
    item.update(overrides)
    table.put_item(Item=item)
    return proposal_id


# ---- claim_proposal ----------------------------------------------------------

def test_claim_proposal_flips_pending_to_applied(aws_env):
    proposal_id = _put_pending(aws_env)
    claimed = claim_proposal(aws_env, "demo-user", proposal_id)
    assert claimed["status"] == "applied"


def test_claim_proposal_second_call_fails(aws_env):
    proposal_id = _put_pending(aws_env)
    claim_proposal(aws_env, "demo-user", proposal_id)
    second = claim_proposal(aws_env, "demo-user", proposal_id)
    assert second is None


def test_claim_proposal_expired_returns_none(aws_env):
    proposal_id = _put_pending(aws_env, ttl_offset=-60)
    assert claim_proposal(aws_env, "demo-user", proposal_id) is None


def test_claim_proposal_missing_returns_none(aws_env):
    assert claim_proposal(aws_env, "demo-user", "does-not-exist") is None


# ---- apply_progression (moto DynamoDB + monkeypatched Hevy calls) -------------

def test_apply_progression_success(aws_env, monkeypatch):
    proposal_id = _put_pending(aws_env)
    monkeypatch.setattr(ap, "get_api_key", lambda ssm_client, param: "fake-key")
    monkeypatch.setattr(ap, "find_routine_for_exercise", lambda api_key, template_id: FAKE_ROUTINE)
    monkeypatch.setattr(ap, "update_exercise_target", lambda *a, **k: {"routine": FAKE_ROUTINE})

    result = apply_progression(proposal_id)

    assert result["exercise_title"] == "Bench Press"
    assert result["weight_kg"] == 22.5
    assert result["reps"] == 9


def test_apply_progression_missing_proposal_never_calls_hevy(aws_env, monkeypatch):
    called = []
    monkeypatch.setattr(ap, "get_api_key", lambda *a, **k: called.append(1) or "fake-key")

    result = apply_progression("does-not-exist")

    assert "error" in result
    assert called == []  # never reached the Hevy credential path


def test_apply_progression_routine_not_found(aws_env, monkeypatch):
    proposal_id = _put_pending(aws_env)
    monkeypatch.setattr(ap, "get_api_key", lambda *a, **k: "fake-key")
    monkeypatch.setattr(ap, "find_routine_for_exercise", lambda *a, **k: None)

    result = apply_progression(proposal_id)
    assert "error" in result


def test_apply_progression_hevy_error_surfaces(aws_env, monkeypatch):
    proposal_id = _put_pending(aws_env)
    monkeypatch.setattr(ap, "get_api_key", lambda *a, **k: "fake-key")
    monkeypatch.setattr(ap, "find_routine_for_exercise", lambda *a, **k: FAKE_ROUTINE)

    def _raise(*a, **k):
        raise HevyAPIError("Hevy API PUT returned 500: boom")
    monkeypatch.setattr(ap, "update_exercise_target", _raise)

    result = apply_progression(proposal_id)
    assert "error" in result


def test_apply_progression_cannot_replay(aws_env, monkeypatch):
    proposal_id = _put_pending(aws_env)
    monkeypatch.setattr(ap, "get_api_key", lambda *a, **k: "fake-key")
    monkeypatch.setattr(ap, "find_routine_for_exercise", lambda *a, **k: FAKE_ROUTINE)
    monkeypatch.setattr(ap, "update_exercise_target", lambda *a, **k: {"routine": FAKE_ROUTINE})

    first = apply_progression(proposal_id)
    second = apply_progression(proposal_id)

    assert "error" not in first
    assert "error" in second
