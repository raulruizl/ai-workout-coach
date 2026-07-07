# D1 — Step Functions state machine wiring B1->B2->B3->B4 (ADR-001: replaces
# Airflow, zero idle cost, native retry/DLQ per state).
#
# Glue steps use the .sync service integration so Step Functions waits for
# job completion natively instead of a manual poll loop. Every state has
# its own Retry (transient errors) and Catch (permanent failure -> SQS DLQ,
# then Fail). CloudWatch alarms on the state machine itself are D3, not here.

resource "aws_iam_role" "pipeline_state_machine" {
  name = "${var.project_name}-pipeline-state-machine"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "states.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

# Least-privilege, scoped per resource — mirrors the pattern used by every
# other component's role (per security-expert review in CLAUDE.md).
resource "aws_iam_role_policy" "pipeline_state_machine" {
  name = "${var.project_name}-pipeline-state-machine"
  role = aws_iam_role.pipeline_state_machine.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "InvokePipelineLambdas"
        Effect = "Allow"
        Action = "lambda:InvokeFunction"
        Resource = [
          aws_lambda_function.extract_hevy_workouts.arn,
          aws_lambda_function.sync_gold_to_dynamodb.arn,
        ]
      },
      {
        Sid    = "RunPipelineGlueJobs"
        Effect = "Allow"
        Action = ["glue:StartJobRun", "glue:GetJobRun", "glue:GetJobRuns", "glue:BatchStopJobRun"]
        Resource = [
          aws_glue_job.bronze_to_silver.arn,
          aws_glue_job.silver_to_gold.arn,
        ]
      },
      {
        Sid      = "SendToDLQOnFailure"
        Effect   = "Allow"
        Action   = "sqs:SendMessage"
        Resource = aws_sqs_queue.pipeline_dlq.arn
      }
    ]
  })
}

locals {
  # Every state's Catch routes here on any error, carrying the failed
  # state's name + error details into the DLQ message.
  failure_catch = [{
    ErrorEquals = ["States.ALL"]
    ResultPath  = "$.error"
    Next        = "NotifyFailure"
  }]
  standard_retry = [{
    ErrorEquals     = ["States.TaskFailed", "States.Timeout"]
    IntervalSeconds = 10
    MaxAttempts     = 2
    BackoffRate     = 2.0
  }]
}

resource "aws_sfn_state_machine" "workout_coach_pipeline" {
  name     = "${var.project_name}-pipeline"
  role_arn = aws_iam_role.pipeline_state_machine.arn

  definition = jsonencode({
    Comment = "Workout Coach medallion pipeline: extract -> bronze->silver -> silver->gold -> sync to DynamoDB"
    StartAt = "ExtractHevyWorkouts"
    States = {
      ExtractHevyWorkouts = {
        Type     = "Task"
        Resource = "arn:aws:states:::lambda:invoke"
        Parameters = {
          FunctionName = aws_lambda_function.extract_hevy_workouts.arn
          "Payload.$"  = "$"
        }
        ResultPath = "$.extract_result"
        Retry      = local.standard_retry
        Catch      = local.failure_catch
        Next       = "HasNewData"
      }
      HasNewData = {
        Type = "Choice"
        Choices = [{
          Variable           = "$.extract_result.Payload.written"
          NumericGreaterThan = 0
          Next               = "BronzeToSilver"
        }]
        Default = "NoNewData"
      }
      NoNewData = {
        Type    = "Succeed"
        Comment = "Extract found no new Hevy events this run — Glue/sync skipped, nothing to process."
      }
      BronzeToSilver = {
        Type     = "Task"
        Resource = "arn:aws:states:::glue:startJobRun.sync"
        Parameters = {
          JobName = aws_glue_job.bronze_to_silver.name
          Arguments = {
            "--TARGET_USER_ID.$" = "$.user_id"
          }
        }
        ResultPath = "$.bronze_to_silver_result"
        Retry      = local.standard_retry
        Catch      = local.failure_catch
        Next       = "SilverToGold"
      }
      SilverToGold = {
        Type     = "Task"
        Resource = "arn:aws:states:::glue:startJobRun.sync"
        Parameters = {
          JobName = aws_glue_job.silver_to_gold.name
          Arguments = {
            "--TARGET_USER_ID.$" = "$.user_id"
          }
        }
        ResultPath = "$.silver_to_gold_result"
        Retry      = local.standard_retry
        Catch      = local.failure_catch
        Next       = "SyncGoldToDynamoDB"
      }
      SyncGoldToDynamoDB = {
        Type     = "Task"
        Resource = "arn:aws:states:::lambda:invoke"
        Parameters = {
          FunctionName = aws_lambda_function.sync_gold_to_dynamodb.arn
          "Payload.$"  = "$"
        }
        ResultPath = "$.sync_result"
        Retry      = local.standard_retry
        Catch      = local.failure_catch
        End        = true
      }
      NotifyFailure = {
        Type     = "Task"
        Resource = "arn:aws:states:::sqs:sendMessage"
        Parameters = {
          QueueUrl        = aws_sqs_queue.pipeline_dlq.url
          "MessageBody.$" = "$"
        }
        Next = "PipelineFailed"
      }
      PipelineFailed = {
        Type  = "Fail"
        Error = "PipelineExecutionFailed"
        Cause = "One or more pipeline states failed — see SQS DLQ for details"
      }
    }
  })
}
