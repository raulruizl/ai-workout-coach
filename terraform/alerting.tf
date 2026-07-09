resource "aws_sqs_queue" "pipeline_dlq" {
  name                      = "${var.project_name}-pipeline-dlq"
  message_retention_seconds = 1209600 # 14 days, max — give room to debug failures
}

resource "aws_sns_topic" "pipeline_alerts" {
  name = "${var.project_name}-pipeline-alerts"
}

# Without a subscription the two alarms below publish into the void —
# nobody learns the pipeline failed. Email requires a one-time click on
# the AWS confirmation mail before deliveries start (same address as the
# weekly report, already SES-verified, but SNS confirmation is separate).
resource "aws_sns_topic_subscription" "pipeline_alerts_email" {
  topic_arn = aws_sns_topic.pipeline_alerts.arn
  protocol  = "email"
  endpoint  = var.notification_email
}

# D3 — CloudWatch alarms on the pipeline. Reuses the existing DLQ + SNS
# topic above rather than standing up a second alerting path (ADR-006).

resource "aws_cloudwatch_metric_alarm" "pipeline_execution_failed" {
  alarm_name          = "${var.project_name}-pipeline-execution-failed"
  alarm_description   = "workout-coach-pipeline Step Functions execution failed"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  period              = 300
  statistic           = "Sum"
  threshold           = 1
  namespace           = "AWS/States"
  metric_name         = "ExecutionsFailed"
  treat_missing_data  = "notBreaching"

  dimensions = {
    StateMachineArn = aws_sfn_state_machine.workout_coach_pipeline.arn
  }

  alarm_actions = [aws_sns_topic.pipeline_alerts.arn]
  ok_actions    = [aws_sns_topic.pipeline_alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "pipeline_dlq_not_empty" {
  alarm_name          = "${var.project_name}-pipeline-dlq-not-empty"
  alarm_description   = "workout-coach-pipeline DLQ has an unprocessed failure message"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  period              = 300
  statistic           = "Maximum"
  threshold           = 1
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  treat_missing_data  = "notBreaching"

  dimensions = {
    QueueName = aws_sqs_queue.pipeline_dlq.name
  }

  alarm_actions = [aws_sns_topic.pipeline_alerts.arn]
  ok_actions    = [aws_sns_topic.pipeline_alerts.arn]
}
