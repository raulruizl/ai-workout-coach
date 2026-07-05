import gzip
import json
import os
import urllib.error
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

import handler as handler_module
from handler import (
    EPOCH,
    HevyAPIError,
    build_bronze_key,
    fetch_all_events,
    fetch_events_page,
    filter_valid_events,
    get_cursor,
    is_valid_event,
    set_cursor,
    write_bronze_batch,
)

BUCKET = "workout-coach-bronze-test"
TABLE_NAME = "workout-coach-stats-test"
PARAM_NAME = "/workout-coach/hevy-api-key-test"

VALID_WORKOUT = {
    "id": "wk_1",
    "title": "Push Day",
    "start_time": "2026-07-01T10:00:00Z",
    "end_time": "2026-07-01T11:00:00Z",
    "exercises": [],
}


# ---- Hevy API client --------------------------------------------------------

def _mock_response(payload: dict):
    mock = MagicMock()
    mock.read.return_value = json.dumps(payload).encode("utf-8")
    mock.__enter__.return_value = mock
    return mock


@patch("handler.urllib.request.urlopen")
def test_fetch_events_page_returns_decoded_json(mock_urlopen):
    mock_urlopen.return_value = _mock_response({"page": 1, "page_count": 1, "events": []})

    result = fetch_events_page("api-key-123", "1970-01-01T00:00:00Z", 1)

    assert result == {"page": 1, "page_count": 1, "events": []}
    request = mock_urlopen.call_args[0][0]
    assert request.get_header("Api-key") == "api-key-123"
    assert "page=1" in request.full_url


@patch("handler.urllib.request.urlopen")
def test_fetch_events_page_raises_on_http_error(mock_urlopen):
    err = urllib.error.HTTPError(url="x", code=401, msg="Unauthorized", hdrs=None, fp=None)
    err.read = MagicMock(return_value=b'{"error": "bad key"}')
    mock_urlopen.side_effect = err

    with pytest.raises(HevyAPIError, match="401"):
        fetch_events_page("bad-key", "1970-01-01T00:00:00Z", 1)


@patch("handler.urllib.request.urlopen")
def test_fetch_events_page_raises_on_transport_error(mock_urlopen):
    mock_urlopen.side_effect = urllib.error.URLError("connection refused")

    with pytest.raises(HevyAPIError, match="unreachable"):
        fetch_events_page("api-key", "1970-01-01T00:00:00Z", 1)


@patch("handler.urllib.request.urlopen")
def test_fetch_all_events_paginates_until_page_count(mock_urlopen):
    page_1 = {"page": 1, "page_count": 2, "events": [{"type": "updated", "workout": {"id": "a"}}]}
    page_2 = {"page": 2, "page_count": 2, "events": [{"type": "updated", "workout": {"id": "b"}}]}
    mock_urlopen.side_effect = [_mock_response(page_1), _mock_response(page_2)]

    events = fetch_all_events("api-key", "1970-01-01T00:00:00Z")

    assert len(events) == 2
    assert mock_urlopen.call_count == 2


@patch("handler.urllib.request.urlopen")
def test_fetch_all_events_single_page_stops_after_one_call(mock_urlopen):
    mock_urlopen.return_value = _mock_response({"page": 1, "page_count": 1, "events": []})

    events = fetch_all_events("api-key", "1970-01-01T00:00:00Z")

    assert events == []
    assert mock_urlopen.call_count == 1


# ---- Source data quality -----------------------------------------------------

def test_valid_updated_event_passes():
    assert is_valid_event({"type": "updated", "workout": VALID_WORKOUT}) is True


def test_updated_event_missing_start_time_fails():
    workout = dict(VALID_WORKOUT)
    del workout["start_time"]
    assert is_valid_event({"type": "updated", "workout": workout}) is False


def test_updated_event_missing_exercises_key_fails():
    workout = dict(VALID_WORKOUT)
    del workout["exercises"]
    assert is_valid_event({"type": "updated", "workout": workout}) is False


def test_valid_deleted_event_passes():
    event = {"type": "deleted", "id": "wk_2", "deleted_at": "2026-07-01T12:00:00Z"}
    assert is_valid_event(event) is True


def test_deleted_event_missing_id_fails():
    event = {"type": "deleted", "deleted_at": "2026-07-01T12:00:00Z"}
    assert is_valid_event(event) is False


def test_unknown_event_type_fails():
    assert is_valid_event({"type": "renamed", "workout": VALID_WORKOUT}) is False


def test_filter_valid_events_drops_only_bad_ones():
    events = [
        {"type": "updated", "workout": VALID_WORKOUT},
        {"type": "updated", "workout": {"id": "wk_bad"}},  # missing start_time/exercises
        {"type": "deleted", "id": "wk_3", "deleted_at": "2026-07-02T00:00:00Z"},
    ]
    result = filter_valid_events(events)
    assert len(result) == 2
    assert result[0]["workout"]["id"] == "wk_1"
    assert result[1]["id"] == "wk_3"


# ---- Bronze S3 writer --------------------------------------------------------

def test_build_bronze_key_matches_partition_convention():
    key = build_bronze_key("demo-user", "2026-07-05", "20260705T120000000000")
    assert key == "workouts/user_id=demo-user/ingest_date=2026-07-05/run_20260705T120000000000.json.gz"


@mock_aws
def test_write_bronze_batch_roundtrips_gzip_json():
    s3_client = boto3.client("s3", region_name="eu-west-1")
    s3_client.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "eu-west-1"})
    events = [{"type": "updated", "workout": {"id": "wk_1"}}]

    key = write_bronze_batch(
        s3_client, BUCKET, "demo-user", "2026-07-05", "run1", "2026-07-05T12:00:00+00:00", events
    )

    obj = s3_client.get_object(Bucket=BUCKET, Key=key)
    payload = json.loads(gzip.decompress(obj["Body"].read()))

    assert payload["schema_version"] == 1
    assert payload["user_id"] == "demo-user"
    assert payload["_ingested_at"] == "2026-07-05T12:00:00+00:00"
    assert payload["events"] == events
    assert obj["ContentEncoding"] == "gzip"


# ---- Sync cursor (DynamoDB) --------------------------------------------------

def _make_stats_table(dynamodb):
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
def test_get_cursor_defaults_to_epoch_when_absent():
    table = _make_stats_table(boto3.resource("dynamodb"))
    assert get_cursor(table, "demo-user") == EPOCH


@mock_aws
def test_set_then_get_cursor_roundtrips():
    table = _make_stats_table(boto3.resource("dynamodb"))
    set_cursor(table, "demo-user", "2026-07-01T00:00:00+00:00")
    assert get_cursor(table, "demo-user") == "2026-07-01T00:00:00+00:00"


@mock_aws
def test_cursor_is_isolated_per_user():
    table = _make_stats_table(boto3.resource("dynamodb"))
    set_cursor(table, "user-a", "2026-07-01T00:00:00+00:00")
    assert get_cursor(table, "user-b") == EPOCH


# ---- Full handler ------------------------------------------------------------

@pytest.fixture
def aws_env():
    with mock_aws():
        os.environ["BRONZE_BUCKET"] = BUCKET
        os.environ["STATS_TABLE_NAME"] = TABLE_NAME
        os.environ["HEVY_API_KEY_PARAM"] = PARAM_NAME

        s3 = boto3.client("s3", region_name="eu-west-1")
        s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "eu-west-1"})

        table = _make_stats_table(boto3.resource("dynamodb"))

        ssm = boto3.client("ssm")
        ssm.put_parameter(Name=PARAM_NAME, Value="real-api-key", Type="SecureString")

        # Reset module-level caches between tests
        handler_module._ssm_client = None
        handler_module._api_key_cache = None

        yield {"s3": s3, "table": table}


@patch("handler.fetch_all_events")
def test_handler_writes_bronze_and_advances_cursor(mock_fetch, aws_env):
    mock_fetch.return_value = [
        {"type": "updated", "workout": {"id": "wk_1", "start_time": "2026-07-01T00:00:00Z", "exercises": []}},
    ]

    result = handler_module.handler({"user_id": "demo-user"}, None)

    assert result["fetched"] == 1
    assert result["written"] == 1
    assert result["since"] == EPOCH

    obj = aws_env["s3"].get_object(Bucket=BUCKET, Key=result["bronze_key"])
    payload = json.loads(gzip.decompress(obj["Body"].read()))
    assert payload["events"][0]["workout"]["id"] == "wk_1"

    cursor_item = aws_env["table"].get_item(Key={"user_id": "demo-user", "stat_type": "SYNC_CURSOR"})["Item"]
    assert cursor_item["since"] != EPOCH


@patch("handler.fetch_all_events")
def test_handler_drops_malformed_events_but_advances_cursor(mock_fetch, aws_env):
    mock_fetch.return_value = [{"type": "updated", "workout": {"id": "wk_bad"}}]  # missing start_time

    result = handler_module.handler({"user_id": "demo-user"}, None)

    assert result["fetched"] == 1
    assert result["written"] == 0
    assert "bronze_key" not in result

    cursor_item = aws_env["table"].get_item(Key={"user_id": "demo-user", "stat_type": "SYNC_CURSOR"})["Item"]
    assert cursor_item["since"] != EPOCH


@patch("handler.fetch_all_events")
def test_handler_uses_ssm_api_key_and_caches_it(mock_fetch, aws_env):
    mock_fetch.return_value = []

    handler_module.handler({"user_id": "demo-user"}, None)
    handler_module.handler({"user_id": "demo-user"}, None)

    assert handler_module._api_key_cache == "real-api-key"
    for call in mock_fetch.call_args_list:
        assert call[0][0] == "real-api-key"
