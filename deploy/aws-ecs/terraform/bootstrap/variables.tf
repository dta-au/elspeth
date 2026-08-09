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
  type    = string
  default = "ap-southeast-2"
}

variable "aws_profile" {
  type = string

  validation {
    condition     = can(regex("^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$", var.aws_profile))
    error_message = "aws_profile must be an explicit shell-safe AWS profile name."
  }
}

variable "iam_lifecycle_aws_profile" {
  type        = string
  description = "AWS profile for the separate principal that owns IAM boundary and role lifecycle only."

  validation {
    condition     = can(regex("^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$", var.iam_lifecycle_aws_profile))
    error_message = "iam_lifecycle_aws_profile must be an explicit shell-safe AWS profile name."
  }
}

variable "backend_state_bucket" {
  type = string

  validation {
    condition = (
      length(var.backend_state_bucket) <= 63
      && !strcontains(var.backend_state_bucket, "..")
      && !endswith(var.backend_state_bucket, "-s3alias")
      && !endswith(var.backend_state_bucket, "--ol-s3")
      && !endswith(var.backend_state_bucket, ".mrap")
      && !endswith(var.backend_state_bucket, "--x-s3")
      && !endswith(var.backend_state_bucket, "--table-s3")
      && can(regex("^elspeth-[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", var.backend_state_bucket))
    )
    error_message = "backend_state_bucket must use the elspeth- prefix and AWS S3 general-purpose bucket grammar, including the 63-character limit and reserved-suffix exclusions."
  }
}

variable "ecr_repository" {
  type = string

  validation {
    condition     = length(var.ecr_repository) <= 256 && can(regex("^elspeth-[a-z0-9]+((\\.|_|__|-+)[a-z0-9]+)*(/[a-z0-9]+((\\.|_|__|-+)[a-z0-9]+)*)*$", var.ecr_repository))
    error_message = "ecr_repository must use the elspeth- prefix and AWS ECR repository-name grammar, and be at most 256 characters."
  }
}

variable "cloudwatch_agent_ecr_repository" {
  type = string

  validation {
    condition     = length(var.cloudwatch_agent_ecr_repository) <= 256 && can(regex("^elspeth-[a-z0-9]+((\\.|_|__|-+)[a-z0-9]+)*(/[a-z0-9]+((\\.|_|__|-+)[a-z0-9]+)*)*$", var.cloudwatch_agent_ecr_repository))
    error_message = "cloudwatch_agent_ecr_repository must use the elspeth- prefix and AWS ECR repository-name grammar, and be at most 256 characters."
  }
}

variable "gateway_ecr_repository" {
  type        = string
  default     = ""
  description = "Exact ECR repository that holds the independently admitted Scenario C gateway image."

  validation {
    condition     = var.gateway_ecr_repository == "" || (length(var.gateway_ecr_repository) <= 256 && can(regex("^elspeth-[a-z0-9]+((\\.|_|__|-+)[a-z0-9]+)*(/[a-z0-9]+((\\.|_|__|-+)[a-z0-9]+)*)*$", var.gateway_ecr_repository)))
    error_message = "gateway_ecr_repository must be empty when Scenario C is not installed, or use the elspeth- prefix and AWS ECR repository-name grammar with at most 256 characters."
  }

  validation {
    condition = contains(
      [0, 4],
      length(compact([
        var.gateway_ecr_repository,
        var.gateway_bearer_secret_arn,
        var.gateway_oauth_client_id_secret_arn,
        var.gateway_oauth_client_secret_secret_arn,
      ])),
    )
    error_message = "Scenario C bootstrap inputs must be either all empty or all set so the execution-role boundary cannot be created with partial gateway authority."
  }
}

variable "gateway_bearer_secret_arn" {
  type        = string
  default     = ""
  description = "Exact commercial-partition Secrets Manager ARN for the Scenario C Web-to-gateway bearer."

  validation {
    condition     = var.gateway_bearer_secret_arn == "" || can(regex("^arn:aws:secretsmanager:${var.aws_region}:${var.aws_account_id}:secret:[A-Za-z0-9/_+=.@-]{1,519}$", var.gateway_bearer_secret_arn))
    error_message = "gateway_bearer_secret_arn must be empty when Scenario C is not installed, or an exact Secrets Manager ARN in this package's aws partition, aws_region, and aws_account_id."
  }
}

variable "gateway_oauth_client_id_secret_arn" {
  type        = string
  default     = ""
  description = "Exact commercial-partition Secrets Manager ARN for the Scenario C gateway OAuth client id."

  validation {
    condition     = var.gateway_oauth_client_id_secret_arn == "" || can(regex("^arn:aws:secretsmanager:${var.aws_region}:${var.aws_account_id}:secret:[A-Za-z0-9/_+=.@-]{1,519}$", var.gateway_oauth_client_id_secret_arn))
    error_message = "gateway_oauth_client_id_secret_arn must be empty when Scenario C is not installed, or an exact Secrets Manager ARN in this package's aws partition, aws_region, and aws_account_id."
  }
}

variable "gateway_oauth_client_secret_secret_arn" {
  type        = string
  default     = ""
  description = "Exact commercial-partition Secrets Manager ARN for the Scenario C gateway OAuth client secret."

  validation {
    condition     = var.gateway_oauth_client_secret_secret_arn == "" || can(regex("^arn:aws:secretsmanager:${var.aws_region}:${var.aws_account_id}:secret:[A-Za-z0-9/_+=.@-]{1,519}$", var.gateway_oauth_client_secret_secret_arn))
    error_message = "gateway_oauth_client_secret_secret_arn must be empty when Scenario C is not installed, or an exact Secrets Manager ARN in this package's aws partition, aws_region, and aws_account_id."
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
