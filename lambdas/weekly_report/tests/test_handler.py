import io
import json
import os
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

from handler import (
    handler,
    invoke_agent,
    render_confirm_links,
    render_html,
    render_markdown_lite,
    run_weekly_report,
    send_report_email,
)

CONFIRM_URL = "https://confirm.example.com/"


# ---- render_confirm_links / render_html ----------------------------------------

def test_render_confirm_links_replaces_placeholder():
    text = "Nice work.\n{{CONFIRM_PROGRESSION:abc-123}}\nSee you next week."
    rendered = render_confirm_links(text, CONFIRM_URL)
    assert "{{CONFIRM_PROGRESSION" not in rendered
    assert f'{CONFIRM_URL}?proposal_id=abc-123' in rendered
    assert "<a href=" in rendered


def test_render_confirm_links_no_placeholder_leaves_text_unchanged():
    text = "No proposal this week."
    assert render_confirm_links(text, CONFIRM_URL) == text


def test_render_confirm_links_ignores_malformed_placeholder():
    text = "{{CONFIRM_PROGRESSION:not valid because spaces}}"
    rendered = render_confirm_links(text, CONFIRM_URL)
    assert "<a href=" not in rendered


def test_render_html_wraps_body_and_converts_newlines():
    html = render_html("line one\nline two", CONFIRM_URL)
    assert "<br>" in html
    assert "<html>" in html


# ---- render_markdown_lite ----------------------------------------------------

def test_render_markdown_lite_converts_bold():
    assert render_markdown_lite("**Increase to 72.5kg**") == "<p><strong>Increase to 72.5kg</strong></p>"


def test_render_markdown_lite_converts_bullet_list():
    rendered = render_markdown_lite("- Leg Press: 72.5kg\n- Leg Curl: 47.5kg")
    assert rendered == "<ul><li>Leg Press: 72.5kg</li><li>Leg Curl: 47.5kg</li></ul>"


def test_render_markdown_lite_separates_paragraphs():
    rendered = render_markdown_lite("First paragraph.\n\nSecond paragraph.")
    assert rendered == "<p>First paragraph.</p>\n<p>Second paragraph.</p>"


def test_render_markdown_lite_converts_headings():
    rendered = render_markdown_lite("## Informe Semanal\n\n### Progresión\n\nTexto normal.")
    assert rendered == "<h2>Informe Semanal</h2>\n<h3>Progresión</h3>\n<p>Texto normal.</p>"


def test_render_markdown_lite_h1_heading():
    assert render_markdown_lite("# Título") == "<h1>Título</h1>"


def test_render_markdown_lite_hash_inside_paragraph_not_treated_as_heading():
    """Only a line that IS its own block, starting with #, becomes a
    heading — '#' appearing mid-paragraph (e.g. a set number like '#3')
    must not be misread as markdown syntax."""
    rendered = render_markdown_lite("Set #3 was the heaviest.")
    assert rendered == "<p>Set #3 was the heaviest.</p>"


def test_render_markdown_lite_does_not_treat_mixed_block_as_list():
    rendered = render_markdown_lite("Intro line\n- not a pure list block")
    assert "<ul>" not in rendered


# ---- invoke_agent -----------------------------------------------------------------

def test_invoke_agent_parses_response_field():
    mock_stream = MagicMock()
    mock_stream.read.return_value = json.dumps({"response": "Your report text"}).encode("utf-8")
    mock_client = MagicMock()
    mock_client.invoke_agent_runtime.return_value = {"response": mock_stream}

    result = invoke_agent(mock_client, "arn:aws:bedrock-agentcore:...:runtime/x")

    assert result == "Your report text"
    mock_client.invoke_agent_runtime.assert_called_once()
    _, kwargs = mock_client.invoke_agent_runtime.call_args
    assert kwargs["agentRuntimeArn"] == "arn:aws:bedrock-agentcore:...:runtime/x"
    assert "runtimeSessionId" in kwargs


# ---- send_report_email -------------------------------------------------------------

@mock_aws
def test_send_report_email_calls_ses():
    ses_client = boto3.client("ses", region_name="eu-west-1")
    ses_client.verify_email_identity(EmailAddress="coach@example.com")
    response = send_report_email(ses_client, "coach@example.com", "coach@example.com", "<p>hi</p>")
    assert "MessageId" in response


# ---- run_weekly_report / handler (end to end) --------------------------------------

@mock_aws
def test_run_weekly_report_sends_email(monkeypatch):
    monkeypatch.setenv("AGENTCORE_AGENT_RUNTIME_ARN", "arn:aws:bedrock-agentcore:eu-west-1:1:runtime/x")
    monkeypatch.setenv("CONFIRM_PROGRESSION_URL", CONFIRM_URL)
    monkeypatch.setenv("SES_SENDER_EMAIL", "coach@example.com")
    monkeypatch.setenv("SES_RECIPIENT_EMAIL", "coach@example.com")

    ses_client = boto3.client("ses", region_name="eu-west-1")
    ses_client.verify_email_identity(EmailAddress="coach@example.com")

    mock_stream = MagicMock()
    mock_stream.read.return_value = json.dumps({"response": "Weekly report body"}).encode("utf-8")
    mock_agentcore = MagicMock()
    mock_agentcore.invoke_agent_runtime.return_value = {"response": mock_stream}

    real_client = boto3.client

    def fake_client(service_name, **kwargs):
        if service_name == "bedrock-agentcore":
            return mock_agentcore
        return real_client(service_name, region_name="eu-west-1")

    with patch("handler.boto3.client", side_effect=fake_client):
        result = run_weekly_report()

    assert result["status"] == "sent"
    assert result["report_length"] == len("Weekly report body")


@mock_aws
def test_handler_wraps_run_weekly_report(monkeypatch):
    monkeypatch.setenv("AGENTCORE_AGENT_RUNTIME_ARN", "arn:aws:bedrock-agentcore:eu-west-1:1:runtime/x")
    monkeypatch.setenv("CONFIRM_PROGRESSION_URL", CONFIRM_URL)
    monkeypatch.setenv("SES_SENDER_EMAIL", "coach@example.com")
    monkeypatch.setenv("SES_RECIPIENT_EMAIL", "coach@example.com")

    ses_client = boto3.client("ses", region_name="eu-west-1")
    ses_client.verify_email_identity(EmailAddress="coach@example.com")

    mock_stream = MagicMock()
    mock_stream.read.return_value = json.dumps({"response": "hi"}).encode("utf-8")
    mock_agentcore = MagicMock()
    mock_agentcore.invoke_agent_runtime.return_value = {"response": mock_stream}

    real_client = boto3.client

    def fake_client(service_name, **kwargs):
        if service_name == "bedrock-agentcore":
            return mock_agentcore
        return real_client(service_name, region_name="eu-west-1")

    with patch("handler.boto3.client", side_effect=fake_client):
        response = handler({}, None)

    assert response["status"] == "sent"
