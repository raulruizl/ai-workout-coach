# B1 — extract Lambda. No third-party deps beyond boto3 (provided by the Lambda
# runtime), so the deployment package is just the single handler.py file.

data "archive_file" "extract_hevy_workouts" {
  type        = "zip"
  source_file = "${path.module}/../lambdas/extract_hevy_workouts/handler.py"
  output_path = "${path.module}/build/extract_hevy_workouts.zip"
}

resource "aws_iam_role" "extract_hevy_workouts" {
  name = "${var.project_name}-extract-hevy-workouts"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

# Least-privilege, scoped per resource — no wildcards beyond what this
# function actually touches (per security-expert review in CLAUDE.md).
resource "aws_iam_role_policy" "extract_hevy_workouts" {
  name = "${var.project_name}-extract-hevy-workouts"
  role = aws_iam_role.extract_hevy_workouts.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "WriteBronze"
        Effect   = "Allow"
        Action   = "s3:PutObject"
        Resource = "${aws_s3_bucket.layer["bronze"].arn}/*"
      },
      {
        Sid      = "ReadHevyApiKey"
        Effect   = "Allow"
        Action   = "ssm:GetParameter"
        Resource = aws_ssm_parameter.hevy_api_key.arn
      },
      {
        Sid      = "ReadWriteSyncCursor"
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:PutItem"]
        Resource = aws_dynamodb_table.workout_coach_stats.arn
      },
      {
        Sid      = "WriteOwnLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${var.aws_region}:*:log-group:/aws/lambda/${var.project_name}-extract-hevy-workouts:*"
      }
    ]
  })
}

resource "aws_lambda_function" "extract_hevy_workouts" {
  function_name    = "${var.project_name}-extract-hevy-workouts"
  role             = aws_iam_role.extract_hevy_workouts.arn
  handler          = "handler.handler"
  runtime          = "python3.12"
  timeout          = 60
  memory_size      = 128
  filename         = data.archive_file.extract_hevy_workouts.output_path
  source_code_hash = data.archive_file.extract_hevy_workouts.output_base64sha256

  environment {
    variables = {
      BRONZE_BUCKET      = aws_s3_bucket.layer["bronze"].bucket
      STATS_TABLE_NAME   = aws_dynamodb_table.workout_coach_stats.name
      HEVY_API_KEY_PARAM = aws_ssm_parameter.hevy_api_key.name
    }
  }
}
