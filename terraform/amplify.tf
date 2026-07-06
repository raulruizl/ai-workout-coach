# G2 — Amplify Hosting for the React chat SPA. Manual deploy (no Git
# auto-build) via frontend/deploy.sh, same pattern as the sibling
# stock-analysis-chatbot project.

resource "aws_amplify_app" "frontend" {
  name = "${var.project_name}-frontend"

  build_spec = <<-EOT
    version: 1
    frontend:
      phases:
        preBuild:
          commands:
            - npm ci
        build:
          commands:
            - npm run build
      artifacts:
        baseDirectory: dist
        files:
          - '**/*'
      cache:
        paths:
          - node_modules/**/*
  EOT
}

resource "aws_amplify_branch" "main" {
  app_id      = aws_amplify_app.frontend.id
  branch_name = "main"

  enable_auto_build = false

  environment_variables = {
    VITE_CHAT_WEBSOCKET_URL = aws_apigatewayv2_stage.prod.invoke_url
  }
}
