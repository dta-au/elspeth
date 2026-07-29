variable "run_id" {
  type = string

  validation {
    condition     = can(regex("^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", var.run_id))
    error_message = "run_id must be a lowercase UUID."
  }
}

variable "scenario_id" {
  type = string

  validation {
    condition     = contains(["A", "B"], var.scenario_id)
    error_message = "scenario_id must be A or B."
  }
}

variable "candidate_sha" {
  type = string

  validation {
    condition     = can(regex("^[0-9a-f]{40}$", var.candidate_sha))
    error_message = "candidate_sha must be a full Git SHA."
  }
}

variable "candidate_image" {
  type = string

  validation {
    condition     = can(regex("@sha256:[0-9a-f]{64}$", var.candidate_image))
    error_message = "candidate_image must be immutable by digest."
  }
}

variable "rollback_baseline_image" {
  type = string

  validation {
    condition     = can(regex("@sha256:[0-9a-f]{64}$", var.rollback_baseline_image))
    error_message = "rollback_baseline_image must be immutable by digest."
  }
}

variable "rollback_baseline_sha" {
  type = string

  validation {
    condition     = can(regex("^[0-9a-f]{40}$", var.rollback_baseline_sha))
    error_message = "rollback_baseline_sha must be a full Git SHA."
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

variable "iam_permissions_boundary_arn" {
  type        = string
  description = "Narrow run-scoped boundary output by bootstrap and required on all generated ECS roles."

  validation {
    condition     = can(regex("^arn:aws:iam::${var.aws_account_id}:policy/elspeth-[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}-ecs-boundary$", var.iam_permissions_boundary_arn))
    error_message = "iam_permissions_boundary_arn must be a run-scoped boundary output by bootstrap in aws_account_id."
  }
}

variable "target_platform" {
  type    = string
  default = "linux/amd64"

  validation {
    condition     = contains(["linux/amd64", "linux/arm64"], var.target_platform)
    error_message = "target_platform must be linux/amd64 or linux/arm64."
  }
}

variable "owner" {
  type = string
}

variable "purpose" {
  type = string
}

variable "cleanup_deadline" {
  type = string
}

variable "cloudwatch_agent_image" {
  type = string

  validation {
    condition     = can(regex("@sha256:[0-9a-f]{64}$", var.cloudwatch_agent_image))
    error_message = "cloudwatch_agent_image must be an immutable digest reference."
  }
}

variable "composer_model" {
  type = string

  validation {
    condition     = startswith(var.composer_model, "bedrock/") && var.composer_model != var.composer_advisor_model
    error_message = "composer_model must be a bedrock/... provider ID distinct from composer_advisor_model."
  }
}

variable "composer_advisor_model" {
  type = string

  validation {
    condition     = startswith(var.composer_advisor_model, "bedrock/")
    error_message = "composer_advisor_model must be a bedrock/... provider ID."
  }
}

variable "bedrock_inference_profile_arns" {
  type = set(string)

  validation {
    condition     = length(var.bedrock_inference_profile_arns) > 0 && alltrue([for arn in var.bedrock_inference_profile_arns : can(regex("^arn:[^:]+:bedrock:[^:]+:[0-9]{12}:inference-profile/", arn))])
    error_message = "bedrock_inference_profile_arns must contain at least one inference-profile ARN."
  }
}

variable "bedrock_foundation_model_arns" {
  type = set(string)

  validation {
    condition     = length(var.bedrock_foundation_model_arns) > 0 && alltrue([for arn in var.bedrock_foundation_model_arns : can(regex("^arn:[^:]+:bedrock:[^:]+::foundation-model/", arn))])
    error_message = "bedrock_foundation_model_arns must contain at least one foundation-model ARN."
  }
}

variable "scenario_tf_dir" {
  type = string

  validation {
    condition     = startswith(var.scenario_tf_dir, "/")
    error_message = "scenario_tf_dir must be an absolute path."
  }
}

variable "scenario_tf_vars" {
  type = string

  validation {
    condition     = startswith(var.scenario_tf_vars, "/")
    error_message = "scenario_tf_vars must be an absolute path."
  }
}

variable "scenario_tf_binding_sha" {
  type = string

  validation {
    condition     = can(regex("^[0-9a-f]{64}$", var.scenario_tf_binding_sha))
    error_message = "scenario_tf_binding_sha must be a SHA-256 digest."
  }
}

variable "scenario_tf_binding_file" {
  type = string

  validation {
    condition     = startswith(var.scenario_tf_binding_file, "/")
    error_message = "scenario_tf_binding_file must be an absolute path."
  }
}

variable "transaction_search_baseline_sha256" {
  type = string

  validation {
    condition     = can(regex("^[0-9a-f]{64}$", var.transaction_search_baseline_sha256))
    error_message = "transaction_search_baseline_sha256 must be a SHA-256 digest."
  }
}

variable "cognito_subject_sub" {
  type    = string
  default = ""

  validation {
    condition = var.scenario_id == "A" ? var.cognito_subject_sub == "" : (
      can(regex("^[\\x20-\\x7e]{1,128}$", var.cognito_subject_sub))
    )
    error_message = "cognito_subject_sub must be empty for Scenario A and a nonempty printable subject of at most 128 characters for Scenario B."
  }
}

variable "alarm_actions" {
  type    = list(string)
  default = []
}

variable "database_name" {
  type    = string
  default = "elspeth"
}

variable "session_database_name" {
  type    = string
  default = "elspeth_session"
}

variable "landscape_database_name" {
  type    = string
  default = "elspeth_landscape"
}

variable "aurora_engine_major_version" {
  type    = string
  default = "16"

  validation {
    condition     = var.aurora_engine_major_version == "16"
    error_message = "this package is currently validated only for Aurora PostgreSQL major version 16."
  }
}

variable "aurora_engine_version" {
  type     = string
  default  = "16.13"
  nullable = false

  validation {
    condition     = var.aurora_engine_version == "16.13"
    error_message = "this package is currently validated only for Aurora PostgreSQL 16.13."
  }
}
