# Confirm-link target for the weekly report email. Executes the actual
# Hevy write for a proposed progression — deliberately NOT routed through
# the agent/model (no chat turn exists to confirm in). The proposal_id in
# the link is itself the bearer token: single-use (conditional DynamoDB
# update) and TTL-bound (see propose_progression.py) — that's the real
# security boundary, not the Function URL's lack of AWS auth.
#
# Function URL (not API Gateway) because this is a single unauthenticated
# GET endpoint clicked from an email client — API Gateway would add a
# routing layer with nothing to route.

data "archive_file" "confirm_progression" {
  type        = "zip"
  source_file = "${path.module}/../lambdas/confirm_progression/handler.py"
  output_path = "${path.module}/build/confirm_progression.zip"
}

resource "aws_iam_role" "confirm_progression" {
  name = "${var.project_name}-confirm-progression"

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
resource "aws_iam_role_policy" "confirm_progression" {
  name = "${var.project_name}-confirm-progression"
  role = aws_iam_role.confirm_progression.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # Claim = conditional UpdateItem on a PROPOSAL#<id> item — the
        # single-use replay guard. No PutItem/GetItem needed here.
        Sid      = "ClaimProgressionProposal"
        Effect   = "Allow"
        Action   = "dynamodb:UpdateItem"
        Resource = aws_dynamodb_table.workout_coach_stats.arn
      },
      {
        Sid      = "ReadHevyApiKeyForProgressionWrite"
        Effect   = "Allow"
        Action   = "ssm:GetParameter"
        Resource = aws_ssm_parameter.hevy_api_key.arn
      },
      {
        Sid      = "WriteOwnLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${var.aws_region}:*:log-group:/aws/lambda/${var.project_name}-confirm-progression:*"
      }
    ]
  })
}

resource "aws_lambda_function" "confirm_progression" {
  function_name    = "${var.project_name}-confirm-progression"
  role             = aws_iam_role.confirm_progression.arn
  handler          = "handler.handler"
  runtime          = "python3.12"
  timeout          = 30
  memory_size      = 128
  filename         = data.archive_file.confirm_progression.output_path
  source_code_hash = data.archive_file.confirm_progression.output_base64sha256

  environment {
    variables = {
      STATS_TABLE_NAME   = aws_dynamodb_table.workout_coach_stats.name
      HEVY_API_KEY_PARAM = aws_ssm_parameter.hevy_api_key.name
      TARGET_USER_ID     = "demo-user"
    }
  }

  depends_on = [aws_cloudwatch_log_group.confirm_progression]
}

# Public, unauthenticated GET endpoint clicked directly from an email
# client — the proposal_id token is the actual gate (see the file header
# comment above), not AWS-level auth.
#
# Originally a Lambda Function URL (authorization_type = NONE), but every
# invoke came back 403 AccessDeniedException even with a correct resource
# policy allowing Principal "*" — some Lambda-side public-access
# restriction on this account rejects public Function URLs specifically
# (verified: account isn't in an AWS Organization, so it isn't an SCP).
# API Gateway HTTP API doesn't hit that same restriction, so that's the
# public surface instead — same no-auth GET, different AWS resource type.
resource "aws_apigatewayv2_api" "confirm_progression" {
  name          = "${var.project_name}-confirm-progression"
  protocol_type = "HTTP"
}

resource "aws_apigatewayv2_integration" "confirm_progression" {
  api_id                 = aws_apigatewayv2_api.confirm_progression.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.confirm_progression.invoke_arn
  integration_method     = "POST"
  payload_format_version = "2.0"
}

# $default (not "GET /") — the confirm link is built as
# "${invoke_url}?proposal_id=..." with no trailing slash, so the request
# path is empty, which "GET /" doesn't match but $default always does.
resource "aws_apigatewayv2_route" "confirm_progression" {
  api_id    = aws_apigatewayv2_api.confirm_progression.id
  route_key = "$default"
  target    = "integrations/${aws_apigatewayv2_integration.confirm_progression.id}"
}

resource "aws_apigatewayv2_stage" "confirm_progression" {
  api_id      = aws_apigatewayv2_api.confirm_progression.id
  name        = "prod"
  auto_deploy = true
}

resource "aws_lambda_permission" "confirm_progression_apigw" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.confirm_progression.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.confirm_progression.execution_arn}/*/*"
}
