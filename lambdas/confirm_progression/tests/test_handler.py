import json
import os
import time
from decimal import Decimal
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

from handler import (
    build_updated_routine_body,
    claim_proposal,
    confirm,
    handler,
)

TABLE_NAME = "workout-coach-stats-test"
PARAM_NAME = "/workout-coach/hevy-api-key-test"
USER_ID = "demo-user"

ROUTINE = {
    "id": "routine_1",
    "title": "Push Day",
    "exercises": [{
        "exercise_template_id": "bench_press",
        "notes": None,
        "superset_id": None,
        "rest_seconds": 90,
        "sets": [
            {"index": 0, "type": "warmup", "weight_kg": 20.0, "reps": 10},
            {"index": 1, "type": "normal", "weight_kg": 60.0, "reps": 8},
        ],
    }],
}


def _mock_response(payload: dict):
    mock = MagicMock()
    mock.read.return_value = json.dumps(payload).encode("utf-8")
    mock.__enter__.return_value = mock
    return mock


@pytest.fixture
def dynamodb_table():
    with mock_aws():
        os.environ["STATS_TABLE_NAME"] = TABLE_NAME
        os.environ["HEVY_API_KEY_PARAM"] = PARAM_NAME
        os.environ["TARGET_USER_ID"] = USER_ID
        table = boto3.resource("dynamodb").create_table(
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
        yield table


def _put_proposal(table, proposal_id, status="pending", ttl_offset=3600):
    table.put_item(Item={
        "user_id": USER_ID,
        "stat_type": f"PROPOSAL#{proposal_id}",
        "exercise_template_id": "bench_press",
        "exercise_title": "Bench Press",
        "current_weight_kg": Decimal("60.0"),
        "proposed_weight_kg": Decimal("62.5"),
        "reps": 8,
        "status": status,
        "ttl": int(time.time()) + ttl_offset,
    })


# ---- build_updated_routine_body ------------------------------------------------

def test_build_updated_routine_body_sets_weight_and_reps():
    body = build_updated_routine_body(ROUTINE, "bench_press", 62.5, 8)
    sets = body["routine"]["exercises"][0]["sets"]
    assert sets[0]["weight_kg"] == 20.0  # warmup untouched
    assert sets[1]["weight_kg"] == 62.5
    assert sets[1]["reps"] == 8
    assert "index" not in sets[1]


# ---- claim_proposal --------------------------------------------------------------

def test_claim_proposal_succeeds_for_pending_unexpired(dynamodb_table):
    _put_proposal(dynamodb_table, "p1")
    result = claim_proposal(dynamodb_table, USER_ID, "p1")
    assert result["status"] == "applied"


def test_claim_proposal_rejects_replay(dynamodb_table):
    _put_proposal(dynamodb_table, "p1")
    claim_proposal(dynamodb_table, USER_ID, "p1")
    result = claim_proposal(dynamodb_table, USER_ID, "p1")
    assert result is None


def test_claim_proposal_rejects_missing(dynamodb_table):
    assert claim_proposal(dynamodb_table, USER_ID, "nonexistent") is None


def test_claim_proposal_rejects_expired(dynamodb_table):
    _put_proposal(dynamodb_table, "p1", ttl_offset=-10)
    assert claim_proposal(dynamodb_table, USER_ID, "p1") is None


# ---- confirm (end to end) -------------------------------------------------------

@patch("handler.get_api_key", return_value="fake-key")
@patch("handler.find_routine_for_exercise", return_value=ROUTINE)
@patch("handler._request", return_value={})
def test_confirm_applies_and_returns_200(mock_request, mock_find, mock_key, dynamodb_table):
    _put_proposal(dynamodb_table, "p1")
    response = confirm("p1")
    assert response["statusCode"] == 200
    assert "62.5kg" in response["body"]
    mock_request.assert_called_once()
    assert mock_request.call_args[0][0] == "PUT"


def test_confirm_expired_proposal_returns_410(dynamodb_table):
    response = confirm("nonexistent")
    assert response["statusCode"] == 410


@patch("handler.get_api_key", return_value="fake-key")
@patch("handler.find_routine_for_exercise", return_value=None)
def test_confirm_no_routine_returns_404(mock_find, mock_key, dynamodb_table):
    _put_proposal(dynamodb_table, "p1")
    response = confirm("p1")
    assert response["statusCode"] == 404


@patch("handler.get_api_key", return_value="fake-key")
@patch("handler.find_routine_for_exercise", return_value=ROUTINE)
@patch("handler._request", side_effect=Exception("unused"))
def test_confirm_hevy_write_failure_returns_502(mock_request, mock_find, mock_key, dynamodb_table):
    from handler import HevyAPIError
    mock_request.side_effect = HevyAPIError("boom")
    _put_proposal(dynamodb_table, "p1")
    response = confirm("p1")
    assert response["statusCode"] == 502


# ---- handler (Function URL event shape) ------------------------------------------

def test_handler_missing_proposal_id_returns_400(dynamodb_table):
    response = handler({"queryStringParameters": None}, None)
    assert response["statusCode"] == 400


@patch("handler.get_api_key", return_value="fake-key")
@patch("handler.find_routine_for_exercise", return_value=ROUTINE)
@patch("handler._request", return_value={})
def test_handler_applies_proposal_from_query_string(mock_request, mock_find, mock_key, dynamodb_table):
    _put_proposal(dynamodb_table, "p1")
    response = handler({"queryStringParameters": {"proposal_id": "p1"}}, None)
    assert response["statusCode"] == 200
