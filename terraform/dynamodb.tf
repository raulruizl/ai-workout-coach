# Sole serving layer for the agent. Item shapes:
#   PK=USER#<user_id>  SK=LATEST         → get_latest_stats
#   PK=USER#<user_id>  SK=WEEK#<date>    → query_workout_history / trend tools
resource "aws_dynamodb_table" "workout_coach_stats" {
  name         = "${var.project_name}-stats"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "user_id"
  range_key    = "stat_type"

  attribute {
    name = "user_id"
    type = "S"
  }

  attribute {
    name = "stat_type"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }
}
