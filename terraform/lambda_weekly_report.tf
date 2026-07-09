# Weekly, unattended invocation of the hypertrophy agent (F1/F2) — no chat
# UI, no per-message trigger. Runs after both the ingest pipeline (D2,
# nightly) and sync_routine_weights (B5, Monday 06:00) so the agent sees
# the freshest data and doesn't itself need to touch Hevy at all — it only
# reads DynamoDB and, for a progression, writes one PROPOSAL#<id> item
# (see agentcore.tf's WriteProgressionProposals statement).

variable "agentcore_agent_runtime_arn" {
  description = "Bedrock AgentCore Runtime ARN — set via `aws bedrock-agentcore-control create-agent-runtime` (see bedrock-agentcore skill), not managed by Terraform"
  type        = string
  default     = "arn:aws:bedrock-agentcore:eu-west-1:338071012815:runtime/workoutCoachAgent-OThZvJGcaQ"
}

data "archive_file" "weekly_report" {
  type        = "zip"
  source_file = "${path.module}/../lambdas/weekly_report/handler.py"
  output_path = "${path.module}/build/weekly_report.zip"
}

resource "aws_iam_role" "weekly_report" {
  name = "${var.project_name}-weekly-report"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

# Least-privilege, scoped per resource — mirrors every other component's IAM pattern.
resource "aws_iam_role_policy" "weekly_report" {
  name = "${var.project_name}-weekly-report"
  role = aws_iam_role.weekly_report.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "InvokeAgentRuntime"
        Effect   = "Allow"
        Action   = "bedrock-agentcore:InvokeAgentRuntime"
        Resource = [var.agentcore_agent_runtime_arn, "${var.agentcore_agent_runtime_arn}/*"]
      },
      {
        Sid      = "SendReportEmail"
        Effect   = "Allow"
        Action   = "ses:SendEmail"
        Resource = aws_ses_email_identity.notification.arn
      },
      {
        Sid      = "WriteOwnLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${var.aws_region}:*:log-group:/aws/lambda/${var.project_name}-weekly-report:*"
      }
    ]
  })
}

resource "aws_lambda_function" "weekly_report" {
  function_name    = "${var.project_name}-weekly-report"
  role             = aws_iam_role.weekly_report.arn
  handler          = "handler.handler"
  runtime          = "python3.12"
  timeout          = 120 # agent invocation is the slow step, not this Lambda's own work
  memory_size      = 128
  filename         = data.archive_file.weekly_report.output_path
  source_code_hash = data.archive_file.weekly_report.output_base64sha256

  environment {
    variables = {
      AGENTCORE_AGENT_RUNTIME_ARN = var.agentcore_agent_runtime_arn
      CONFIRM_PROGRESSION_URL     = aws_apigatewayv2_stage.confirm_progression.invoke_url
      SES_SENDER_EMAIL            = var.notification_email
      SES_RECIPIENT_EMAIL         = var.notification_email
    }
  }

  depends_on = [aws_cloudwatch_log_group.weekly_report]
}

resource "aws_iam_role" "weekly_report_scheduler" {
  name = "${var.project_name}-weekly-report-scheduler"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "weekly_report_scheduler" {
  name = "${var.project_name}-weekly-report-scheduler"
  role = aws_iam_role.weekly_report_scheduler.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "InvokeWeeklyReport"
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = aws_lambda_function.weekly_report.arn
    }]
  })
}

# 07:00 Monday — after sync_routine_weights (06:00 Monday) so the routine
# is already current if the report happens to mention it.
resource "aws_scheduler_schedule" "weekly_report" {
  name = "${var.project_name}-weekly-report"

  flexible_time_window {
    mode = "OFF"
  }

  schedule_expression          = "cron(0 7 ? * MON *)"
  schedule_expression_timezone = "Europe/Madrid"

  target {
    arn      = aws_lambda_function.weekly_report.arn
    role_arn = aws_iam_role.weekly_report_scheduler.arn

    input = jsonencode({})
  }
}
