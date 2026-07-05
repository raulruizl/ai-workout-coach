resource "aws_sqs_queue" "pipeline_dlq" {
  name                      = "${var.project_name}-pipeline-dlq"
  message_retention_seconds = 1209600 # 14 days, max — give room to debug failures
}

resource "aws_sns_topic" "pipeline_alerts" {
  name = "${var.project_name}-pipeline-alerts"
}
