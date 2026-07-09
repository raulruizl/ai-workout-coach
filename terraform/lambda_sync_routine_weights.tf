# B5 — deterministic Hevy routine-weight sync (ADR-007). No LLM in the
# loop, no confirmation gate: mirrors the user's own last-logged
# max_weight_kg into the routine template, closing the gap where Hevy
# never writes a completed workout's weight back into the routine on its
# own. Runs weekly, decoupled from both the ingest pipeline and the agent.

data "archive_file" "sync_routine_weights" {
  type        = "zip"
  source_file = "${path.module}/../lambdas/sync_routine_weights/handler.py"
  output_path = "${path.module}/build/sync_routine_weights.zip"
}

resource "aws_iam_role" "sync_routine_weights" {
  name = "${var.project_name}-sync-routine-weights"

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
resource "aws_iam_role_policy" "sync_routine_weights" {
  name = "${var.project_name}-sync-routine-weights"
  role = aws_iam_role.sync_routine_weights.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadLatestStats"
        Effect   = "Allow"
        Action   = "dynamodb:GetItem"
        Resource = aws_dynamodb_table.workout_coach_stats.arn
      },
      {
        Sid      = "ReadHevyApiKey"
        Effect   = "Allow"
        Action   = "ssm:GetParameter"
        Resource = aws_ssm_parameter.hevy_api_key.arn
      },
      {
        Sid      = "WriteOwnLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${var.aws_region}:*:log-group:/aws/lambda/${var.project_name}-sync-routine-weights:*"
      }
    ]
  })
}

resource "aws_lambda_function" "sync_routine_weights" {
  function_name    = "${var.project_name}-sync-routine-weights"
  role             = aws_iam_role.sync_routine_weights.arn
  handler          = "handler.handler"
  runtime          = "python3.12"
  timeout          = 60
  memory_size      = 128
  filename         = data.archive_file.sync_routine_weights.output_path
  source_code_hash = data.archive_file.sync_routine_weights.output_base64sha256

  environment {
    variables = {
      STATS_TABLE_NAME   = aws_dynamodb_table.workout_coach_stats.name
      HEVY_API_KEY_PARAM = aws_ssm_parameter.hevy_api_key.name
    }
  }

  depends_on = [aws_cloudwatch_log_group.sync_routine_weights]
}

resource "aws_iam_role" "sync_routine_weights_scheduler" {
  name = "${var.project_name}-sync-routine-weights-scheduler"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "sync_routine_weights_scheduler" {
  name = "${var.project_name}-sync-routine-weights-scheduler"
  role = aws_iam_role.sync_routine_weights_scheduler.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "InvokeSyncRoutineWeights"
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = aws_lambda_function.sync_routine_weights.arn
    }]
  })
}

# Runs after the nightly pipeline (23:00) has had time to land the week's
# data in DynamoDB — weekly, not daily, since routine drift only matters
# once per training week, not once per pipeline run.
resource "aws_scheduler_schedule" "sync_routine_weights_weekly" {
  name = "${var.project_name}-sync-routine-weights-weekly"

  flexible_time_window {
    mode = "OFF"
  }

  schedule_expression          = "cron(0 6 ? * MON *)"
  schedule_expression_timezone = "Europe/Madrid"

  target {
    arn      = aws_lambda_function.sync_routine_weights.arn
    role_arn = aws_iam_role.sync_routine_weights_scheduler.arn

    input = jsonencode({ user_id = "demo-user" })
  }
}
