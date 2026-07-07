# G1 — API Gateway WebSocket + Lambda bridge to Bedrock AgentCore.
#
# WebSocket (not REST) per ADR-005: needed so the server can push
# "dashboard_update" messages to the client mid-conversation later, not
# just answer request/response. Today's bridge sends one full "chat_token"
# message per prompt (no per-token streaming yet — agent.py's entrypoint
# returns one final response) — upgrading to real streaming only touches
# this Lambda + agent.py, not this transport layer.
#
# The AgentCore Runtime itself has no Terraform resource (see the
# bedrock-agentcore skill) — it was created via CLI, its ARN is wired in
# as a variable.

variable "agentcore_agent_runtime_arn" {
  description = "Bedrock AgentCore Runtime ARN — set via `aws bedrock-agentcore-control create-agent-runtime` (see bedrock-agentcore skill), not managed by Terraform"
  type        = string
  default     = "arn:aws:bedrock-agentcore:eu-west-1:338071012815:runtime/workoutCoachAgent-OThZvJGcaQ"
}

# WebSocket connection -> AgentCore runtimeSessionId mapping. TTL cleans up
# stale connections that never got a proper $disconnect (network drop etc).
resource "aws_dynamodb_table" "ws_connections" {
  name         = "${var.project_name}-ws-connections"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "connection_id"

  attribute {
    name = "connection_id"
    type = "S"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }
}

data "archive_file" "chat_bridge" {
  type        = "zip"
  source_file = "${path.module}/../lambdas/chat_bridge/handler.py"
  output_path = "${path.module}/build/chat_bridge.zip"
}

resource "aws_iam_role" "chat_bridge" {
  name = "${var.project_name}-chat-bridge"

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
resource "aws_iam_role_policy" "chat_bridge" {
  name = "${var.project_name}-chat-bridge"
  role = aws_iam_role.chat_bridge.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadWriteConnections"
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:DeleteItem"]
        Resource = aws_dynamodb_table.ws_connections.arn
      },
      {
        Sid      = "InvokeAgentRuntime"
        Effect   = "Allow"
        Action   = "bedrock-agentcore:InvokeAgentRuntime"
        Resource = [var.agentcore_agent_runtime_arn, "${var.agentcore_agent_runtime_arn}/*"]
      },
      {
        # $default dispatches an async self-invocation of this same function
        # to escape API Gateway's ~29s WebSocket integration wait — see
        # dispatch_default/handle_default in handler.py.
        Sid      = "SelfInvokeAsync"
        Effect   = "Allow"
        Action   = "lambda:InvokeFunction"
        Resource = "arn:aws:lambda:${var.aws_region}:*:function:${var.project_name}-chat-bridge"
      },
      {
        Sid      = "PushToWebSocketConnections"
        Effect   = "Allow"
        Action   = "execute-api:ManageConnections"
        Resource = "${aws_apigatewayv2_api.chat.execution_arn}/*"
      },
      {
        Sid      = "WriteOwnLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${var.aws_region}:*:log-group:/aws/lambda/${var.project_name}-chat-bridge:*"
      }
    ]
  })
}

resource "aws_lambda_function" "chat_bridge" {
  function_name    = "${var.project_name}-chat-bridge"
  role             = aws_iam_role.chat_bridge.arn
  handler          = "handler.handler"
  runtime          = "python3.12"
  timeout          = 120 # worker path is no longer bound by API Gateway's ~29s wait; margin for the multi-agent chain
  memory_size      = 128
  filename         = data.archive_file.chat_bridge.output_path
  source_code_hash = data.archive_file.chat_bridge.output_base64sha256

  environment {
    variables = {
      CONNECTIONS_TABLE_NAME      = aws_dynamodb_table.ws_connections.name
      AGENTCORE_AGENT_RUNTIME_ARN = var.agentcore_agent_runtime_arn
    }
  }
}

resource "aws_apigatewayv2_api" "chat" {
  name                       = "${var.project_name}-chat"
  protocol_type              = "WEBSOCKET"
  route_selection_expression = "$request.body.action"
}

resource "aws_apigatewayv2_integration" "chat_bridge" {
  api_id             = aws_apigatewayv2_api.chat.id
  integration_type   = "AWS_PROXY"
  integration_uri    = aws_lambda_function.chat_bridge.invoke_arn
  integration_method = "POST"
}

resource "aws_apigatewayv2_route" "connect" {
  api_id    = aws_apigatewayv2_api.chat.id
  route_key = "$connect"
  target    = "integrations/${aws_apigatewayv2_integration.chat_bridge.id}"
}

resource "aws_apigatewayv2_route" "disconnect" {
  api_id    = aws_apigatewayv2_api.chat.id
  route_key = "$disconnect"
  target    = "integrations/${aws_apigatewayv2_integration.chat_bridge.id}"
}

resource "aws_apigatewayv2_route" "default" {
  api_id    = aws_apigatewayv2_api.chat.id
  route_key = "$default"
  target    = "integrations/${aws_apigatewayv2_integration.chat_bridge.id}"
}

resource "aws_apigatewayv2_stage" "prod" {
  api_id      = aws_apigatewayv2_api.chat.id
  name        = "prod"
  auto_deploy = true
}

resource "aws_lambda_permission" "chat_bridge_invoke" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.chat_bridge.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.chat.execution_arn}/*/*"
}
