resource "aws_s3_bucket_public_access_block" "layer" {
  for_each = aws_s3_bucket.layer

  bucket = each.value.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "layer" {
  for_each = aws_s3_bucket.layer

  bucket = each.value.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Bronze is the only immutable raw copy — protect it from accidental overwrite/delete.
resource "aws_s3_bucket_versioning" "bronze" {
  bucket = aws_s3_bucket.layer["bronze"].id

  versioning_configuration {
    status = "Enabled"
  }
}
