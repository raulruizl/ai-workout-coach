# F1 — Bedrock AgentCore Runtime scaffold. Terraform manages the ECR repo +
# execution IAM role; the runtime itself has no Terraform resource yet
# (checked: hashicorp/aws 5.x has none) — created via `aws
# bedrock-agentcore-control create-agent-runtime` CLI after the image is
# pushed. See the bedrock-agentcore skill for the full deploy loop.

resource "aws_ecr_repository" "agent" {
  name                 = "${var.project_name}-agent"
  image_tag_mutability = "IMMUTABLE" # no exceptions — mutable tags = supply-chain risk

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }
}

resource "aws_ecr_lifecycle_policy" "agent" {
  repository = aws_ecr_repository.agent.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep last 5 images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 5
      }
      action = { type = "expire" }
    }]
  })
}

resource "aws_iam_role" "agentcore" {
  name = "${var.project_name}-agentcore-runtime-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "bedrock-agentcore.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

# Least-privilege, scoped per resource where IAM allows it. GetAuthorizationToken
# cannot be scoped to a resource (IAM requirement) — BatchGetImage and
# GetDownloadUrlForLayer are scoped to this repo only.
resource "aws_iam_role_policy" "agentcore" {
  name = "${var.project_name}-agentcore-runtime-policy"
  role = aws_iam_role.agentcore.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ECRAuth"
        Effect   = "Allow"
        Action   = "ecr:GetAuthorizationToken"
        Resource = "*"
      },
      {
        Sid      = "ECRPull"
        Effect   = "Allow"
        Action   = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
        Resource = aws_ecr_repository.agent.arn
      },
      {
        # Cross-region inference profiles fan out to foundation-model ARNs in
        # multiple regions under the hood — scoping tighter than "*" risks
        # AccessDenied against regions IAM can't be told about ahead of time.
        Sid      = "BedrockInvoke"
        Effect   = "Allow"
        Action   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
        Resource = "*"
      },
      {
        Sid      = "ReadAgentStats"
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:Query"]
        Resource = aws_dynamodb_table.workout_coach_stats.arn
      },
      {
        Sid      = "WriteOwnLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${var.aws_region}:*:log-group:/aws/bedrock-agentcore/*"
      }
    ]
  })
}
