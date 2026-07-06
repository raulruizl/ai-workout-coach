# B3 — silver -> gold transform, Glue Python Shell. Weekly aggregates
# (volume, est_1RM); stall detection is an agent tool (F2), not computed here.

resource "aws_s3_object" "silver_to_gold_script" {
  bucket = aws_s3_bucket.glue_scripts.id
  key    = "silver_to_gold/script.py"
  source = "${path.module}/../glue_jobs/silver_to_gold/script.py"
  etag   = filemd5("${path.module}/../glue_jobs/silver_to_gold/script.py")
}

resource "aws_iam_role" "silver_to_gold" {
  name = "${var.project_name}-silver-to-gold"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "glue.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

# Least-privilege, scoped per resource — mirrors B2's IAM pattern.
resource "aws_iam_role_policy" "silver_to_gold" {
  name = "${var.project_name}-silver-to-gold"
  role = aws_iam_role.silver_to_gold.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadScript"
        Effect   = "Allow"
        Action   = "s3:GetObject"
        Resource = "${aws_s3_bucket.glue_scripts.arn}/*"
      },
      {
        Sid      = "ReadSilver"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:ListBucket"]
        Resource = [aws_s3_bucket.layer["silver"].arn, "${aws_s3_bucket.layer["silver"].arn}/*"]
      },
      {
        Sid      = "WriteGold"
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:ListBucket"]
        Resource = [aws_s3_bucket.layer["gold"].arn, "${aws_s3_bucket.layer["gold"].arn}/*"]
      },
      {
        Sid      = "WriteOwnLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${var.aws_region}:*:log-group:/aws-glue/python-jobs/*"
      }
    ]
  })
}

resource "aws_glue_job" "silver_to_gold" {
  name         = "${var.project_name}-silver-to-gold"
  role_arn     = aws_iam_role.silver_to_gold.arn
  glue_version = "3.0"
  max_capacity = 0.0625 # smallest Python Shell size — KB/MB-scale, not Spark territory

  command {
    name            = "pythonshell"
    python_version  = "3.9"
    script_location = "s3://${aws_s3_bucket.glue_scripts.bucket}/${aws_s3_object.silver_to_gold_script.key}"
  }

  default_arguments = {
    "--additional-python-modules" = "pandas,pyarrow"
    "--TempDir"                   = "s3://${aws_s3_bucket.glue_scripts.bucket}/tmp/"
    "--SILVER_BUCKET"             = aws_s3_bucket.layer["silver"].bucket
    "--GOLD_BUCKET"               = aws_s3_bucket.layer["gold"].bucket
    "--TARGET_USER_ID"            = "demo-user"
  }
}
