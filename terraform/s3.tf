resource "random_id" "bucket_suffix" {
  byte_length = 4
}

locals {
  bucket_suffix = random_id.bucket_suffix.hex
  layers        = ["bronze", "silver", "gold"]
}

resource "aws_s3_bucket" "layer" {
  for_each = toset(local.layers)

  bucket = "${var.project_name}-${each.key}-${local.bucket_suffix}"
}
