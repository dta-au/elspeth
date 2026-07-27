variable "run_id" {
  type = string

  validation {
    condition     = can(regex("^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", var.run_id))
    error_message = "run_id must be a lowercase UUID."
  }
}

variable "aws_account_id" {
  type      = string
  sensitive = true

  validation {
    condition     = can(regex("^[0-9]{12}$", var.aws_account_id))
    error_message = "aws_account_id must contain 12 digits."
  }
}

variable "aws_region" {
  type = string
}

variable "backend_state_bucket" {
  type = string
}

variable "ecr_repository" {
  type = string
}

variable "cloudwatch_agent_ecr_repository" {
  type = string
}

variable "owner" {
  type = string
}

variable "purpose" {
  type = string
}

variable "cleanup_deadline" {
  type        = string
  description = "ISO-8601 timestamp after which this disposable installation should be removed."

  validation {
    condition     = can(formatdate("YYYY-MM-DD'T'hh:mm:ssZ", var.cleanup_deadline))
    error_message = "cleanup_deadline must be an ISO-8601 timestamp."
  }
}
