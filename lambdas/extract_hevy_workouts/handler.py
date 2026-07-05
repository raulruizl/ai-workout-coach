"""Extract Lambda (B1): pulls incremental Hevy workout events into the bronze zone.

Invocation input (from Step Functions or a test invoke):
    {"user_id": "demo-user"}

Env vars:
    BRONZE_BUCKET          - target S3 bucket for raw events
    STATS_TABLE_NAME       - DynamoDB table holding sync cursors + agent stats
    HEVY_API_KEY_PARAM     - SSM parameter name holding the Hevy API key
"""
import gzip
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import boto3

logger = logging.getLogger(__name__)

HEVY_BASE_URL = "https://api.hevyapp.com"
MAX_PAGE_SIZE = 10
SCHEMA_VERSION = 1
CURSOR_SK = "SYNC_CURSOR"
EPOCH = "1970-01-01T00:00:00Z"

_ssm_client = None
_api_key_cache = None


class HevyAPIError(Exception):
    """Raised on any non-2xx response or transport failure — lets the
    caller (Step Functions) retry/DLQ rather than silently losing data."""


# ---- Hevy API client -------------------------------------------------------

def fetch_events_page(api_key: str, since: str, page: int, page_size: int = MAX_PAGE_SIZE) -> dict:
    """Fetch one page of /v1/workouts/events. Returns the decoded JSON envelope:
    {"page": int, "page_count": int, "events": [...]}."""
    query = urllib.parse.urlencode({"since": since, "page": page, "pageSize": page_size})
    url = f"{HEVY_BASE_URL}/v1/workouts/events?{query}"
    request = urllib.request.Request(url, headers={"api-key": api_key, "Accept": "application/json"})

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise HevyAPIError(f"Hevy API returned {exc.code} for page {page}: {body}") from exc
    except urllib.error.URLError as exc:
        raise HevyAPIError(f"Hevy API unreachable fetching page {page}: {exc.reason}") from exc


def fetch_all_events(api_key: str, since: str) -> list[dict]:
    """Paginate through /v1/workouts/events since the given ISO 8601 timestamp,
    returning the flattened list of event objects across all pages."""
    events: list[dict] = []
    page = 1
    page_count = 1

    while page <= page_count:
        envelope = fetch_events_page(api_key, since, page)
        events.extend(envelope.get("events", []))
        page_count = envelope.get("page_count", 1)
        page += 1

    return events


# ---- Source data quality ----------------------------------------------------

def is_valid_event(event: dict) -> bool:
    event_type = event.get("type")

    if event_type == "updated":
        workout = event.get("workout") or {}
        return bool(workout.get("id")) and bool(workout.get("start_time")) and "exercises" in workout

    if event_type == "deleted":
        return bool(event.get("id")) and bool(event.get("deleted_at"))

    return False


def filter_valid_events(events: list[dict]) -> list[dict]:
    valid = []
    for event in events:
        if is_valid_event(event):
            valid.append(event)
        else:
            logger.warning("Dropping malformed Hevy event: %r", event)
    return valid


# ---- Bronze S3 writer -------------------------------------------------------

def build_bronze_key(user_id: str, ingest_date: str, run_id: str) -> str:
    return f"workouts/user_id={user_id}/ingest_date={ingest_date}/run_{run_id}.json.gz"


def write_bronze_batch(s3_client, bucket: str, user_id: str, ingest_date: str, run_id: str,
                        ingested_at: str, events: list[dict]) -> str:
    """Gzips the wrapped event batch and puts it to the bronze bucket. Returns the S3 key."""
    payload = {
        "schema_version": SCHEMA_VERSION,
        "_ingested_at": ingested_at,
        "user_id": user_id,
        "events": events,
    }
    body = gzip.compress(json.dumps(payload).encode("utf-8"))
    key = build_bronze_key(user_id, ingest_date, run_id)

    s3_client.put_object(
        Bucket=bucket,
        Key=key,
        Body=body,
        ContentType="application/json",
        ContentEncoding="gzip",
    )
    return key


# ---- Sync cursor (DynamoDB) -------------------------------------------------
# Reuses the agent-facing stats table under a distinct SK so it never collides
# with the LATEST/WEEK#<date> items the agent reads.

def get_cursor(table, user_id: str) -> str:
    response = table.get_item(Key={"user_id": user_id, "stat_type": CURSOR_SK})
    item = response.get("Item")
    return item["since"] if item else EPOCH


def set_cursor(table, user_id: str, since: str) -> None:
    table.put_item(Item={"user_id": user_id, "stat_type": CURSOR_SK, "since": since})


# ---- Lambda entrypoint ------------------------------------------------------

def _get_api_key(ssm_client, parameter_name: str) -> str:
    global _api_key_cache
    if _api_key_cache is None:
        response = ssm_client.get_parameter(Name=parameter_name, WithDecryption=True)
        _api_key_cache = response["Parameter"]["Value"]
    return _api_key_cache


def handler(event, _context):
    user_id = event["user_id"]

    bronze_bucket = os.environ["BRONZE_BUCKET"]
    table_name = os.environ["STATS_TABLE_NAME"]
    api_key_param = os.environ["HEVY_API_KEY_PARAM"]

    global _ssm_client
    if _ssm_client is None:
        _ssm_client = boto3.client("ssm")
    api_key = _get_api_key(_ssm_client, api_key_param)

    dynamodb = boto3.resource("dynamodb")
    table = dynamodb.Table(table_name)
    s3_client = boto3.client("s3")

    run_started_at = datetime.now(timezone.utc)
    since = get_cursor(table, user_id)

    raw_events = fetch_all_events(api_key, since)
    valid_events = filter_valid_events(raw_events)

    result = {"user_id": user_id, "since": since, "fetched": len(raw_events), "written": 0}

    if valid_events:
        ingest_date = run_started_at.strftime("%Y-%m-%d")
        run_id = run_started_at.strftime("%Y%m%dT%H%M%S%f")
        key = write_bronze_batch(
            s3_client,
            bronze_bucket,
            user_id,
            ingest_date,
            run_id,
            run_started_at.isoformat(),
            valid_events,
        )
        result["written"] = len(valid_events)
        result["bronze_key"] = key

    set_cursor(table, user_id, run_started_at.isoformat())

    return result
