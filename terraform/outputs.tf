output "s3_bucket_names" {
  description = "Bronze/silver/gold bucket names, keyed by layer"
  value       = { for k, b in aws_s3_bucket.layer : k => b.bucket }
}

output "hevy_api_key_parameter_name" {
  description = "SSM parameter name to populate with the real Hevy API key"
  value       = aws_ssm_parameter.hevy_api_key.name
}

output "dynamodb_table_name" {
  description = "DynamoDB table name for agent-facing stats"
  value       = aws_dynamodb_table.workout_coach_stats.name
}

output "sqs_dlq_url" {
  description = "Dead-letter queue URL for pipeline failures"
  value       = aws_sqs_queue.pipeline_dlq.url
}

output "sns_alert_topic_arn" {
  description = "SNS topic ARN for pipeline failure alerts"
  value       = aws_sns_topic.pipeline_alerts.arn
}

output "extract_lambda_name" {
  description = "Extract Lambda function name (B1)"
  value       = aws_lambda_function.extract_hevy_workouts.function_name
}

output "bronze_to_silver_job_name" {
  description = "Glue Python Shell job name (B2)"
  value       = aws_glue_job.bronze_to_silver.name
}

output "silver_to_gold_job_name" {
  description = "Glue Python Shell job name (B3)"
  value       = aws_glue_job.silver_to_gold.name
}

output "sync_gold_to_dynamodb_lambda_name" {
  description = "Sync Lambda function name (B4)"
  value       = aws_lambda_function.sync_gold_to_dynamodb.function_name
}

output "pipeline_state_machine_arn" {
  description = "Step Functions state machine ARN (D1)"
  value       = aws_sfn_state_machine.workout_coach_pipeline.arn
}

output "agent_ecr_repository_url" {
  description = "ECR repo URL for the AgentCore agent container (F1)"
  value       = aws_ecr_repository.agent.repository_url
}

output "agentcore_role_arn" {
  description = "IAM role ARN for the AgentCore Runtime execution role (F1)"
  value       = aws_iam_role.agentcore.arn
}
