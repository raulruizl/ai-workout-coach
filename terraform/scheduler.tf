# D2 — EventBridge Scheduler daily cron for the pipeline (ADR-007).
#
# Uses EventBridge Scheduler (not a classic EventBridge Rule) specifically
# for its native ScheduleExpressionTimezone — a classic Rule's
# schedule_expression is UTC-only and would need manual DST offset math
# for "23:00 Europe/Madrid", drifting twice a year. Scheduler handles the
# DST transition itself.
#
# Glue jobs (the real cost driver) only run when ExtractHevyWorkouts finds
# new events — see the HasNewData Choice state in step_functions.tf.

resource "aws_iam_role" "pipeline_scheduler" {
  name = "${var.project_name}-pipeline-scheduler"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

# Least-privilege, scoped to exactly this state machine — mirrors every
# other component's IAM pattern (per security-expert review in CLAUDE.md).
resource "aws_iam_role_policy" "pipeline_scheduler" {
  name = "${var.project_name}-pipeline-scheduler"
  role = aws_iam_role.pipeline_scheduler.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "StartPipelineExecution"
      Effect   = "Allow"
      Action   = "states:StartExecution"
      Resource = aws_sfn_state_machine.workout_coach_pipeline.arn
    }]
  })
}

resource "aws_scheduler_schedule" "pipeline_daily" {
  name = "${var.project_name}-pipeline-daily"

  flexible_time_window {
    mode = "OFF"
  }

  schedule_expression          = "cron(0 23 * * ? *)"
  schedule_expression_timezone = "Europe/Madrid"

  target {
    arn      = aws_sfn_state_machine.workout_coach_pipeline.arn
    role_arn = aws_iam_role.pipeline_scheduler.arn

    input = jsonencode({ user_id = "demo-user" })
  }
}
