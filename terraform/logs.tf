# Explicit log groups for every Lambda, with retention — otherwise Lambda
# auto-creates its log group on first invoke with "Never expire", so logs
# accumulate indefinitely.
#
# Glue (/aws-glue/python-jobs/*) and AgentCore (/aws/bedrock-agentcore/*)
# log groups are AWS-managed shared prefixes, not 1:1 with our jobs —
# left alone here rather than risk clipping retention for other tenants
# of those prefixes.

locals {
  log_retention_days = 3
}

resource "aws_cloudwatch_log_group" "extract_hevy_workouts" {
  name              = "/aws/lambda/${var.project_name}-extract-hevy-workouts"
  retention_in_days = local.log_retention_days
}

resource "aws_cloudwatch_log_group" "sync_gold_to_dynamodb" {
  name              = "/aws/lambda/${var.project_name}-sync-gold-to-dynamodb"
  retention_in_days = local.log_retention_days
}

resource "aws_cloudwatch_log_group" "sync_routine_weights" {
  name              = "/aws/lambda/${var.project_name}-sync-routine-weights"
  retention_in_days = local.log_retention_days
}

resource "aws_cloudwatch_log_group" "weekly_report" {
  name              = "/aws/lambda/${var.project_name}-weekly-report"
  retention_in_days = local.log_retention_days
}

resource "aws_cloudwatch_log_group" "confirm_progression" {
  name              = "/aws/lambda/${var.project_name}-confirm-progression"
  retention_in_days = local.log_retention_days
}
