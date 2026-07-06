import json
import time
from unittest.mock import MagicMock

import boto3
import pytest
from moto import mock_aws

import handler as h

TABLE_NAME = "connections-test"


def _make_table(dynamodb):
    dynamodb.create_table(
        TableName=TABLE_NAME,
        KeySchema=[{"AttributeName": "connection_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "connection_id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    return dynamodb.Table(TABLE_NAME)


@pytest.fixture
def aws_env(monkeypatch):
    monkeypatch.setenv("CONNECTIONS_TABLE_NAME", TABLE_NAME)
    monkeypatch.setenv("AGENTCORE_AGENT_RUNTIME_ARN", "arn:aws:bedrock-agentcore:eu-west-1:123:runtime/fake")
    with mock_aws():
        table = _make_table(boto3.resource("dynamodb", region_name="eu-west-1"))
        yield table


# ---- message builders ---------------------------------------------------------

def test_build_chat_message_shape():
    msg = h.build_chat_message("hello")
    assert msg == {"type": "chat_token", "content": "hello", "done": True}


def test_build_error_message_shape():
    msg = h.build_error_message("bad")
    assert msg == {"type": "error", "content": "bad"}


# ---- connect / disconnect (moto DynamoDB) --------------------------------------

def test_handle_connect_persists_session(aws_env):
    result = h.handle_connect("conn-1")
    assert result == {"statusCode": 200}

    item = aws_env.get_item(Key={"connection_id": "conn-1"})["Item"]
    assert "session_id" in item
    assert len(item["session_id"]) >= 33  # AgentCore's runtimeSessionId minimum


def test_handle_disconnect_removes_connection(aws_env):
    h.handle_connect("conn-1")
    h.handle_disconnect("conn-1")
    assert aws_env.get_item(Key={"connection_id": "conn-1"}).get("Item") is None


def test_get_session_id_returns_none_when_missing(aws_env):
    assert h.get_session_id("no-such-conn") is None


def test_get_session_id_roundtrips(aws_env):
    h.handle_connect("conn-1")
    session_id = h.get_session_id("conn-1")
    assert session_id is not None


# ---- invoke_agent ---------------------------------------------------------------

def test_invoke_agent_parses_response_body():
    fake_client = MagicMock()
    fake_stream = MagicMock()
    fake_stream.read.return_value = json.dumps({"response": "hi there"}).encode("utf-8")
    fake_client.invoke_agent_runtime.return_value = {"response": fake_stream}

    result = h.invoke_agent(fake_client, "arn:fake", "session-1", "hello")

    assert result == "hi there"
    fake_client.invoke_agent_runtime.assert_called_once_with(
        agentRuntimeArn="arn:fake",
        runtimeSessionId="session-1",
        payload=json.dumps({"prompt": "hello"}),
        contentType="application/json",
        accept="application/json",
    )


# ---- handle_default (moto DynamoDB + monkeypatched AgentCore/management API) ----

def _event(connection_id: str, body: dict) -> dict:
    return {
        "requestContext": {
            "routeKey": "$default", "connectionId": connection_id,
            "domainName": "abc123.execute-api.eu-west-1.amazonaws.com", "stage": "prod",
        },
        "body": json.dumps(body),
    }


def test_handle_default_pushes_chat_message(aws_env, monkeypatch):
    h.handle_connect("conn-1")

    fake_management = MagicMock()
    monkeypatch.setattr(h, "_management_client", lambda event: fake_management)

    fake_agentcore = MagicMock()
    fake_stream = MagicMock()
    fake_stream.read.return_value = json.dumps({"response": "you're doing great"}).encode("utf-8")
    fake_agentcore.invoke_agent_runtime.return_value = {"response": fake_stream}
    monkeypatch.setattr(boto3, "client", lambda service, **kw: fake_agentcore if service == "bedrock-agentcore" else MagicMock())

    result = h.handle_default(_event("conn-1", {"prompt": "how am I doing?"}), "conn-1")

    assert result == {"statusCode": 200}
    fake_management.post_to_connection.assert_called_once()
    _, kwargs = fake_management.post_to_connection.call_args
    pushed = json.loads(kwargs["Data"])
    assert pushed == {"type": "chat_token", "content": "you're doing great", "done": True}


def test_handle_default_missing_prompt_pushes_error(aws_env, monkeypatch):
    h.handle_connect("conn-1")
    fake_management = MagicMock()
    monkeypatch.setattr(h, "_management_client", lambda event: fake_management)

    result = h.handle_default(_event("conn-1", {}), "conn-1")

    assert result == {"statusCode": 400}
    fake_management.post_to_connection.assert_called_once()


def test_handle_default_unknown_connection_returns_gone(aws_env):
    result = h.handle_default(_event("never-connected", {"prompt": "hi"}), "never-connected")
    assert result == {"statusCode": 410}


def test_handle_default_agent_error_pushes_error_message(aws_env, monkeypatch):
    h.handle_connect("conn-1")
    fake_management = MagicMock()
    monkeypatch.setattr(h, "_management_client", lambda event: fake_management)
    monkeypatch.setattr(h, "invoke_agent", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(boto3, "client", lambda *a, **k: MagicMock())

    result = h.handle_default(_event("conn-1", {"prompt": "hi"}), "conn-1")

    assert result == {"statusCode": 500}
    _, kwargs = fake_management.post_to_connection.call_args
    pushed = json.loads(kwargs["Data"])
    assert pushed["type"] == "error"


# ---- top-level handler routing --------------------------------------------------

def test_handler_routes_connect(aws_env, monkeypatch):
    called = []
    monkeypatch.setattr(h, "handle_connect", lambda conn_id: called.append(conn_id) or {"statusCode": 200})

    event = {"requestContext": {"routeKey": "$connect", "connectionId": "conn-1"}}
    result = h.handler(event, None)

    assert result == {"statusCode": 200}
    assert called == ["conn-1"]


def test_handler_routes_disconnect(aws_env, monkeypatch):
    called = []
    monkeypatch.setattr(h, "handle_disconnect", lambda conn_id: called.append(conn_id) or {"statusCode": 200})

    event = {"requestContext": {"routeKey": "$disconnect", "connectionId": "conn-1"}}
    h.handler(event, None)

    assert called == ["conn-1"]
