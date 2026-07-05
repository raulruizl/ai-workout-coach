provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = "workout-coach"
      ManagedBy   = "terraform"
      Environment = var.environment
    }
  }
}
