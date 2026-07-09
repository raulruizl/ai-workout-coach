import json
import os
from decimal import Decimal
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

import handler as handler_module
from handler import (
    HevyAPIError,
    build_updated_routine_body,
    current_target_weight,
    fetch_latest_exercises,
    find_routine_for_exercise,
    handler,
    sync,
    sync_exercise,
)

TABLE_NAME = "workout-coach-stats-test"
PARAM_NAME = "/workout-coach/hevy-api-key-test"
USER_ID = "demo-user"

ROUTINE = {
    "id": "routine_1",
    "title": "Push Day",
    "exercises": [
        {
            "exercise_template_id": "bench_press",
            "notes": None,
            "superset_id": None,
            "rest_seconds": 90,
            "sets": [
                {"index": 0, "type": "warmup", "weight_kg": 20.0, "reps": 10},
                {"index": 1, "type": "normal", "weight_kg": 60.0, "reps": 8},
                {"index": 2, "type": "normal", "weight_kg": 60.0, "reps": 8},
            ],
        }
    ],
}


def _mock_response(payload: dict):
    mock = MagicMock()
    mock.read.return_value = json.dumps(payload).encode("utf-8")
    mock.__enter__.return_value = mock
    return mock


# ---- current_target_weight ---------------------------------------------------

def test_current_target_weight_returns_normal_set_weight():
    assert current_target_weight(ROUTINE, "bench_press") == 60.0


def test_current_target_weight_none_for_missing_exercise():
    assert current_target_weight(ROUTINE, "squat") is None


def test_current_target_weight_ignores_warmup_only():
    routine = {"exercises": [{
        "exercise_template_id": "bench_press",
        "sets": [{"type": "warmup", "weight_kg": 20.0}],
    }]}
    assert current_target_weight(routine, "bench_press") is None


# ---- build_updated_routine_body ----------------------------------------------

def test_build_updated_routine_body_updates_only_normal_sets_of_target_exercise():
    body = build_updated_routine_body(ROUTINE, "bench_press", 65.0)
    sets = body["routine"]["exercises"][0]["sets"]
    assert sets[0]["weight_kg"] == 20.0  # warmup untouched
    assert sets[1]["weight_kg"] == 65.0
    assert sets[2]["weight_kg"] == 65.0
    # reps preserved, not overwritten
    assert sets[1]["reps"] == 8
    # index field stripped (Hevy write-schema quirk)
    assert "index" not in sets[0]


def test_build_updated_routine_body_leaves_other_exercises_alone():
    routine = {
        "title": "Push Day",
        "exercises": [
            ROUTINE["exercises"][0],
            {
                "exercise_template_id": "shoulder_press",
                "sets": [{"index": 0, "type": "normal", "weight_kg": 30.0, "reps": 10}],
                "rest_seconds": 60,
            },
        ],
    }
    body = build_updated_routine_body(routine, "bench_press", 65.0)
    assert body["routine"]["exercises"][1]["sets"][0]["weight_kg"] == 30.0


# ---- sync_exercise ------------------------------------------------------------

@patch("handler.find_routine_for_exercise")
def test_sync_exercise_no_routine_found(mock_find):
    mock_find.return_value = None
    result = sync_exercise("key", "bench_press", 65.0)
    assert result == {"exercise_template_id": "bench_press", "status": "no_routine_found"}


@patch("handler.find_routine_for_exercise")
def test_sync_exercise_already_in_sync_skips_write(mock_find):
    mock_find.return_value = ROUTINE
    with patch("handler._request") as mock_request:
        result = sync_exercise("key", "bench_press", 60.0)
        mock_request.assert_not_called()
    assert result["status"] == "already_in_sync"


@patch("handler.find_routine_for_exercise")
def test_sync_exercise_updates_when_weight_differs(mock_find):
    mock_find.return_value = ROUTINE
    with patch("handler._request") as mock_request:
        mock_request.return_value = {}
        result = sync_exercise("key", "bench_press", 65.0)
        mock_request.assert_called_once()
        args, kwargs = mock_request.call_args
        assert args[0] == "PUT"
        assert args[1] == "/v1/routines/routine_1"
    assert result == {
        "exercise_template_id": "bench_press",
        "status": "updated",
        "previous_weight_kg": 60.0,
        "new_weight_kg": 65.0,
    }


# ---- find_routine_for_exercise (pagination) ------------------------------------

@patch("handler.urllib.request.urlopen")
def test_find_routine_for_exercise_paginates(mock_urlopen):
    mock_urlopen.side_effect = [
        _mock_response({"routines": [{"exercises": [{"exercise_template_id": "squat"}]}], "page_count": 2}),
        _mock_response({"routines": [{"id": "r2", "exercises": [{"exercise_template_id": "bench_press"}]}], "page_count": 2}),
    ]
    routine = find_routine_for_exercise("key", "bench_press")
    assert routine["id"] == "r2"
    assert mock_urlopen.call_count == 2


# ---- fetch_latest_exercises + sync (end to end) --------------------------------

@pytest.fixture
def dynamodb_table():
    with mock_aws():
        os.environ["STATS_TABLE_NAME"] = TABLE_NAME
        os.environ["HEVY_API_KEY_PARAM"] = PARAM_NAME
        client = boto3.resource("dynamodb")
        table = client.create_table(
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


def test_fetch_latest_exercises_returns_empty_when_no_item(dynamodb_table):
    assert fetch_latest_exercises(dynamodb_table, USER_ID) == []


def test_fetch_latest_exercises_returns_exercises(dynamodb_table):
    dynamodb_table.put_item(Item={
        "user_id": USER_ID,
        "stat_type": "LATEST",
        "exercises": [{"exercise_template_id": "bench_press", "max_weight_kg": Decimal("65.0")}],
    })
    exercises = fetch_latest_exercises(dynamodb_table, USER_ID)
    assert exercises[0]["exercise_template_id"] == "bench_press"


def test_sync_no_exercises_returns_zero_checked(dynamodb_table):
    result = sync(USER_ID)
    assert result == {"user_id": USER_ID, "exercises_checked": 0, "updated": 0, "results": []}


@patch("handler.get_api_key", return_value="fake-key")
@patch("handler.find_routine_for_exercise")
def test_sync_updates_stale_exercise(mock_find, mock_get_key, dynamodb_table):
    dynamodb_table.put_item(Item={
        "user_id": USER_ID,
        "stat_type": "LATEST",
        "exercises": [
            {"exercise_template_id": "bench_press", "max_weight_kg": Decimal("65.0")},
            {"exercise_template_id": "bodyweight_dip", "max_weight_kg": None},
        ],
    })
    mock_find.return_value = ROUTINE
    with patch("handler._request") as mock_request:
        mock_request.return_value = {}
        result = sync(USER_ID)

    assert result["exercises_checked"] == 1  # bodyweight exercise skipped (no max_weight_kg)
    assert result["updated"] == 1
    assert result["results"][0]["status"] == "updated"


def test_sync_exercise_error_is_captured_not_raised(dynamodb_table):
    dynamodb_table.put_item(Item={
        "user_id": USER_ID,
        "stat_type": "LATEST",
        "exercises": [{"exercise_template_id": "bench_press", "max_weight_kg": Decimal("65.0")}],
    })
    with patch("handler.get_api_key", return_value="fake-key"), \
         patch("handler.find_routine_for_exercise", side_effect=HevyAPIError("boom")):
        result = sync(USER_ID)

    assert result["results"][0]["status"] == "error"
    assert "boom" in result["results"][0]["error"]


def test_handler_wraps_sync(dynamodb_table):
    response = handler({"user_id": USER_ID}, None)
    assert response["user_id"] == USER_ID
    assert response["exercises_checked"] == 0
