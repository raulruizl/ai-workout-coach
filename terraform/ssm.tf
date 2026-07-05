# Placeholder value only. Set the real Hevy API key out-of-band after apply:
#   aws ssm put-parameter --name /workout-coach/hevy-api-key --type SecureString \
#     --value "<real-key>" --overwrite
# Never pass the real key through Terraform (would land in state/plan output).
resource "aws_ssm_parameter" "hevy_api_key" {
  name        = "/workout-coach/hevy-api-key"
  description = "Hevy API key (Pro tier) — extract Lambda reads this at invoke time"
  type        = "SecureString"
  value       = "REPLACE_ME"

  lifecycle {
    ignore_changes = [value]
  }
}
