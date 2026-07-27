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

variable "aws_profile" {
  type = string

  validation {
    condition     = can(regex("^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$", var.aws_profile))
    error_message = "aws_profile must be an explicit shell-safe AWS profile name."
  }
}

variable "backend_state_bucket" {
  type = string

  validation {
    condition     = can(regex("^elspeth-[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", var.backend_state_bucket))
    error_message = "backend_state_bucket must use the elspeth- prefix and a valid lowercase S3 bucket name."
  }
}

variable "ecr_repository" {
  type = string

  validation {
    condition     = can(regex("^elspeth-[a-z0-9][a-z0-9._/-]{1,254}$", var.ecr_repository))
    error_message = "ecr_repository must use the elspeth- prefix."
  }
}

variable "cloudwatch_agent_ecr_repository" {
  type = string

  validation {
    condition     = can(regex("^elspeth-[a-z0-9][a-z0-9._/-]{1,254}$", var.cloudwatch_agent_ecr_repository))
    error_message = "cloudwatch_agent_ecr_repository must use the elspeth- prefix."
  }
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
