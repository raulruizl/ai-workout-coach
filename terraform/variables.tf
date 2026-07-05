variable "aws_region" {
  description = "AWS region for all resources"
  type        = string
  default     = "eu-west-1"
}

variable "environment" {
  description = "Deployment environment tag"
  type        = string
  default     = "dev"
}

variable "project_name" {
  description = "Short project identifier used as a resource-name prefix"
  type        = string
  default     = "workout-coach"
}
