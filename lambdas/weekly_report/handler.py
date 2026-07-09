"""weekly_report Lambda: invokes the Bedrock AgentCore hypertrophy agent
for its weekly analysis, renders the response as an HTML email, and sends
it via SES. No chat turn exists — this is the only entrypoint that talks
to the agent. If the agent proposed a progression this run, its report
text contains a literal `{{CONFIRM_PROGRESSION:<proposal_id>}}` placeholder
(see agent/agent.py's system prompt) which this Lambda turns into a real
link pointing at the confirm_progression Function URL — clicking it is
the only way the proposal gets applied, and that click executes entirely
outside the model (see lambdas/confirm_progression).

Invocation input:
    {} (or {"user_id": "demo-user"} — reserved for future multi-user use)

Env vars:
    AGENTCORE_AGENT_RUNTIME_ARN, CONFIRM_PROGRESSION_URL,
    SES_SENDER_EMAIL, SES_RECIPIENT_EMAIL
"""
from __future__ import annotations

import json
import logging
import os
import re
import uuid

import boto3

logger = logging.getLogger(__name__)

_CONFIRM_PLACEHOLDER_RE = re.compile(r"\{\{CONFIRM_PROGRESSION:([0-9a-fA-F-]+)\}\}")


# ---- Agent invocation -------------------------------------------------------

def invoke_agent(agentcore_client, runtime_arn: str) -> str:
    raw = agentcore_client.invoke_agent_runtime(
        agentRuntimeArn=runtime_arn,
        runtimeSessionId=str(uuid.uuid4()),
        payload=json.dumps({}),
        contentType="application/json",
        accept="application/json",
    )
    result = json.loads(raw["response"].read())
    return result.get("response", "")


# ---- Report rendering --------------------------------------------------------

def render_confirm_links(report_text: str, confirm_base_url: str) -> str:
    """Replace every {{CONFIRM_PROGRESSION:<id>}} placeholder with a real
    clickable link. Any placeholder the model mangled (malformed id, extra
    text) simply won't match the regex and is left as plain text — better
    to show a broken-looking line than construct a link from something
    that isn't a real proposal_id."""
    def _replace(match: re.Match) -> str:
        proposal_id = match.group(1)
        url = f"{confirm_base_url}?proposal_id={proposal_id}"
        return f'<a href="{url}">Confirm this weight update</a>'

    return _CONFIRM_PLACEHOLDER_RE.sub(_replace, report_text)


_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_HEADING_RE = re.compile(r"^(#{1,3})\s+(.*)$")


def render_markdown_lite(text: str) -> str:
    """Minimal markdown -> HTML: '#'/'##'/'###' headings, **bold**, '- '
    bullet lists, blank-line paragraphs. The model writes plain
    markdown-ish text (see agent.py's system prompt); this is the only
    place that turns it into real HTML — no external markdown dependency
    for a handful of constructs."""
    text = _BOLD_RE.sub(r"<strong>\1</strong>", text)

    blocks = []
    for para in text.split("\n\n"):
        lines = [line for line in para.split("\n") if line.strip()]
        if not lines:
            continue

        heading_match = _HEADING_RE.match(lines[0].strip())
        if heading_match and len(lines) == 1:
            level = len(heading_match.group(1))
            blocks.append(f"<h{level}>{heading_match.group(2)}</h{level}>")
            continue

        if all(line.strip().startswith("- ") for line in lines):
            items = "".join(f"<li>{line.strip()[2:]}</li>" for line in lines)
            blocks.append(f"<ul>{items}</ul>")
        else:
            blocks.append(f"<p>{'<br>'.join(lines)}</p>")
    return "\n".join(blocks)


def render_html(report_text: str, confirm_base_url: str) -> str:
    linked = render_confirm_links(report_text, confirm_base_url)
    # Report is the model's own output, not third-party user input, so no
    # HTML-escaping pass here — render_markdown_lite only ever emits the
    # fixed tags it constructs itself.
    body = render_markdown_lite(linked)
    return f"""<!doctype html><html><head><meta charset="utf-8"></head>
<body style="font-family: sans-serif; max-width: 640px; margin: 2rem auto; line-height: 1.5;">
{body}
</body></html>"""


# ---- SES send ------------------------------------------------------------------

def send_report_email(ses_client, sender: str, recipient: str, html_body: str) -> dict:
    return ses_client.send_email(
        Source=sender,
        Destination={"ToAddresses": [recipient]},
        Message={
            "Subject": {"Data": "Your weekly hypertrophy report"},
            "Body": {"Html": {"Data": html_body}},
        },
    )


# ---- Entrypoint ------------------------------------------------------------------

def run_weekly_report() -> dict:
    runtime_arn = os.environ["AGENTCORE_AGENT_RUNTIME_ARN"]
    confirm_base_url = os.environ["CONFIRM_PROGRESSION_URL"]
    sender = os.environ["SES_SENDER_EMAIL"]
    recipient = os.environ["SES_RECIPIENT_EMAIL"]

    agentcore_client = boto3.client("bedrock-agentcore")
    report_text = invoke_agent(agentcore_client, runtime_arn)

    html_body = render_html(report_text, confirm_base_url)

    ses_client = boto3.client("ses")
    send_report_email(ses_client, sender, recipient, html_body)

    return {"status": "sent", "report_length": len(report_text)}


def handler(event, _context):
    del event
    return run_weekly_report()
