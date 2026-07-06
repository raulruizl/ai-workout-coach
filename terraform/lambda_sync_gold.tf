# B4 — sync Lambda: gold S3 parquet -> DynamoDB (sole serving layer, ADR-004).
# Needs pandas/pyarrow to read parquet, which aren't in the base Lambda
# runtime — uses AWS's managed AWSSDKPandas layer instead of bundling
# ~70MB of deps into the deployment zip.

data "archive_file" "sync_gold_to_dynamodb" {
  type        = "zip"
  source_file = "${path.module}/../lambdas/sync_gold_to_dynamodb/handler.py"
  output_path = "${path.module}/build/sync_gold_to_dynamodb.zip"
}

resource "aws_iam_role" "sync_gold_to_dynamodb" {
  name = "${var.project_name}-sync-gold-to-dynamodb"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

# Least-privilege, scoped per resource — mirrors B1's IAM pattern.
resource "aws_iam_role_policy" "sync_gold_to_dynamodb" {
  name = "${var.project_name}-sync-gold-to-dynamodb"
  role = aws_iam_role.sync_gold_to_dynamodb.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadGold"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:ListBucket"]
        Resource = [aws_s3_bucket.layer["gold"].arn, "${aws_s3_bucket.layer["gold"].arn}/*"]
      },
      {
        Sid      = "WriteAgentStats"
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem", "dynamodb:BatchWriteItem"]
        Resource = aws_dynamodb_table.workout_coach_stats.arn
      },
      {
        Sid      = "WriteOwnLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${var.aws_region}:*:log-group:/aws/lambda/${var.project_name}-sync-gold-to-dynamodb:*"
      }
    ]
  })
}

resource "aws_lambda_function" "sync_gold_to_dynamodb" {
  function_name    = "${var.project_name}-sync-gold-to-dynamodb"
  role             = aws_iam_role.sync_gold_to_dynamodb.arn
  handler          = "handler.handler"
  runtime          = "python3.12"
  timeout          = 60
  memory_size      = 256
  filename         = data.archive_file.sync_gold_to_dynamodb.output_path
  source_code_hash = data.archive_file.sync_gold_to_dynamodb.output_base64sha256

  layers = ["arn:aws:lambda:${var.aws_region}:336392948345:layer:AWSSDKPandas-Python312:29"]

  environment {
    variables = {
      GOLD_BUCKET      = aws_s3_bucket.layer["gold"].bucket
      STATS_TABLE_NAME = aws_dynamodb_table.workout_coach_stats.name
    }
  }
}
