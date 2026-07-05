# B2 — bronze -> silver transform, Glue Python Shell (not Spark, see CLAUDE.md ADR).

resource "aws_s3_bucket" "glue_scripts" {
  bucket = "${var.project_name}-glue-scripts-${local.bucket_suffix}"
}

resource "aws_s3_bucket_public_access_block" "glue_scripts" {
  bucket = aws_s3_bucket.glue_scripts.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "glue_scripts" {
  bucket = aws_s3_bucket.glue_scripts.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_object" "bronze_to_silver_script" {
  bucket = aws_s3_bucket.glue_scripts.id
  key    = "bronze_to_silver/script.py"
  source = "${path.module}/../glue_jobs/bronze_to_silver/script.py"
  etag   = filemd5("${path.module}/../glue_jobs/bronze_to_silver/script.py")
}

resource "aws_iam_role" "bronze_to_silver" {
  name = "${var.project_name}-bronze-to-silver"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "glue.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

# Least-privilege, scoped per resource — mirrors B1's IAM pattern.
resource "aws_iam_role_policy" "bronze_to_silver" {
  name = "${var.project_name}-bronze-to-silver"
  role = aws_iam_role.bronze_to_silver.id

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
        Sid      = "ReadBronze"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:ListBucket"]
        Resource = [aws_s3_bucket.layer["bronze"].arn, "${aws_s3_bucket.layer["bronze"].arn}/*"]
      },
      {
        Sid      = "WriteSilver"
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:ListBucket"]
        Resource = [aws_s3_bucket.layer["silver"].arn, "${aws_s3_bucket.layer["silver"].arn}/*"]
      },
      {
        Sid      = "ReadWriteSilverCursor"
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:PutItem"]
        Resource = aws_dynamodb_table.workout_coach_stats.arn
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

resource "aws_glue_job" "bronze_to_silver" {
  name         = "${var.project_name}-bronze-to-silver"
  role_arn     = aws_iam_role.bronze_to_silver.arn
  glue_version = "3.0"
  max_capacity = 0.0625 # smallest Python Shell size — this job is KB/MB-scale, not Spark territory

  command {
    name            = "pythonshell"
    python_version  = "3.9"
    script_location = "s3://${aws_s3_bucket.glue_scripts.bucket}/${aws_s3_object.bronze_to_silver_script.key}"
  }

  default_arguments = {
    "--additional-python-modules" = "pandas,pyarrow"
    "--TempDir"                   = "s3://${aws_s3_bucket.glue_scripts.bucket}/tmp/"
    "--BRONZE_BUCKET"             = aws_s3_bucket.layer["bronze"].bucket
    "--SILVER_BUCKET"             = aws_s3_bucket.layer["silver"].bucket
    "--STATS_TABLE_NAME"          = aws_dynamodb_table.workout_coach_stats.name
    "--TARGET_USER_ID"            = "demo-user"
  }
}
