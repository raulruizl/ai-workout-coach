"""Chat bridge Lambda (G1): API Gateway WebSocket <-> Bedrock AgentCore.

Not true per-token streaming yet (agent.py's entrypoint returns one final
response, doesn't stream chunks) — each user message gets back a single
"chat_token" message with the full answer. The WebSocket transport is
still the right call per ADR-005: it lets the UI receive server-pushed
messages at all (a plain REST endpoint can't), which streaming and any
future "dashboard_update" push both need. Upgrading to real per-token
streaming later only touches this Lambda + agent.py, not the transport.

Routes (API Gateway WebSocket routeKey):
    $connect    - mint a runtimeSessionId for this connection, persist it
    $disconnect - clean up the connection record
    $default    - {"prompt": "..."} -> invoke AgentCore, push the answer back

Env vars:
    CONNECTIONS_TABLE_NAME, AGENTCORE_AGENT_RUNTIME_ARN
"""
from __future__ import annotations

import json
import os
import time
import uuid

import boto3

_CONNECTION_TTL_SECONDS = 3600  # 1 hour — stale connections self-clean


def _table():
    table_name = os.environ["CONNECTIONS_TABLE_NAME"]
    region = os.environ.get("AWS_REGION", "eu-west-1")
    return boto3.resource("dynamodb", region_name=region).Table(table_name)


def _management_client(event: dict):
    domain = event["requestContext"]["domainName"]
    stage = event["requestContext"]["stage"]
    region = os.environ.get("AWS_REGION", "eu-west-1")
    return boto3.client("apigatewaymanagementapi", endpoint_url=f"https://{domain}/{stage}", region_name=region)


def new_session_id() -> str:
    return str(uuid.uuid4())


def handle_connect(connection_id: str) -> dict:
    session_id = new_session_id()
    _table().put_item(Item={
        "connection_id": connection_id,
        "session_id": session_id,
        "ttl": int(time.time()) + _CONNECTION_TTL_SECONDS,
    })
    return {"statusCode": 200}


def handle_disconnect(connection_id: str) -> dict:
    _table().delete_item(Key={"connection_id": connection_id})
    return {"statusCode": 200}


def get_session_id(connection_id: str) -> str | None:
    response = _table().get_item(Key={"connection_id": connection_id})
    item = response.get("Item")
    return item["session_id"] if item else None


def invoke_agent(agentcore_client, runtime_arn: str, session_id: str, prompt: str) -> str:
    raw = agentcore_client.invoke_agent_runtime(
        agentRuntimeArn=runtime_arn,
        runtimeSessionId=session_id,
        payload=json.dumps({"prompt": prompt}),
        contentType="application/json",
        accept="application/json",
    )
    result = json.loads(raw["response"].read())
    return result.get("response", "")


def build_chat_message(content: str) -> dict:
    return {"type": "chat_token", "content": content, "done": True}


def build_error_message(content: str) -> dict:
    return {"type": "error", "content": content}


def push_to_connection(management_client, connection_id: str, message: dict) -> None:
    management_client.post_to_connection(ConnectionId=connection_id, Data=json.dumps(message).encode("utf-8"))


def handle_default(event: dict, connection_id: str) -> dict:
    session_id = get_session_id(connection_id)
    if session_id is None:
        return {"statusCode": 410}  # GoneException equivalent — connection not tracked

    management_client = _management_client(event)

    try:
        body = json.loads(event.get("body") or "{}")
        prompt = (body.get("prompt") or "").strip()
        if not prompt:
            push_to_connection(management_client, connection_id, build_error_message("prompt is required"))
            return {"statusCode": 400}

        agentcore_client = boto3.client("bedrock-agentcore", region_name=os.environ.get("AWS_REGION", "eu-west-1"))
        answer = invoke_agent(agentcore_client, os.environ["AGENTCORE_AGENT_RUNTIME_ARN"], session_id, prompt)
        push_to_connection(management_client, connection_id, build_chat_message(answer))
        return {"statusCode": 200}
    except Exception as exc:  # noqa: BLE001 — any failure still needs a message pushed to the client
        push_to_connection(management_client, connection_id, build_error_message(str(exc)))
        return {"statusCode": 500}


def handler(event, _context):
    route_key = event["requestContext"]["routeKey"]
    connection_id = event["requestContext"]["connectionId"]

    if route_key == "$connect":
        return handle_connect(connection_id)
    if route_key == "$disconnect":
        return handle_disconnect(connection_id)
    return handle_default(event, connection_id)
