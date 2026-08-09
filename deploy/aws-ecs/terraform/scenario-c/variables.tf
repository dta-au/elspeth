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
    condition     = var.scenario_id == "C"
    error_message = "this root is reserved for Scenario C."
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
    condition = can(regex(
      "^${var.aws_account_id}\\.dkr\\.ecr\\.${var.aws_region}\\.amazonaws\\.com/${replace(var.candidate_ecr_repository, ".", "\\.")}@sha256:[0-9a-f]{64}$",
      var.candidate_image,
    ))
    error_message = "candidate_image must be a digest in the explicitly approved account, region, and ECR repository."
  }
}

variable "candidate_ecr_repository" {
  type        = string
  description = "Exact application-image ECR repository output by the shared bootstrap."

  validation {
    condition     = length(var.candidate_ecr_repository) <= 256 && can(regex("^elspeth-[a-z0-9]+((\\.|_|__|-+)[a-z0-9]+)*(/[a-z0-9]+((\\.|_|__|-+)[a-z0-9]+)*)*$", var.candidate_ecr_repository))
    error_message = "candidate_ecr_repository must be the bootstrap-created elspeth-prefixed AWS ECR repository name and be at most 256 characters."
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
  description = "AWS profile for the separate principal that owns IAM role lifecycle only."

  validation {
    condition     = can(regex("^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$", var.iam_lifecycle_aws_profile))
    error_message = "iam_lifecycle_aws_profile must be an explicit shell-safe AWS profile name."
  }
}

variable "iam_permissions_boundary_arn" {
  type        = string
  description = "Narrow run-scoped boundary output by bootstrap and required on every generated ECS role."

  validation {
    # Pins THIS run's boundary, not merely a boundary-shaped ARN: any
    # UUID previously matched, so a scenario could be applied under a
    # sibling run's (differently scoped) boundary without complaint.
    condition     = var.iam_permissions_boundary_arn == "arn:aws:iam::${var.aws_account_id}:policy/elspeth-${var.run_id}-ecs-boundary"
    error_message = "iam_permissions_boundary_arn must be exactly this run's boundary output by bootstrap in aws_account_id."
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
  type        = string
  description = "ISO-8601 timestamp after which this disposable installation should be removed."

  validation {
    condition     = can(formatdate("YYYY-MM-DD'T'hh:mm:ssZ", var.cleanup_deadline))
    error_message = "cleanup_deadline must be an ISO-8601 timestamp."
  }
}

variable "cloudwatch_agent_image" {
  type        = string
  description = "Immutable digest reference for the retained shell-bearing CloudWatch agent image."

  validation {
    condition = can(regex(
      "^${var.aws_account_id}\\.dkr\\.ecr\\.${var.aws_region}\\.amazonaws\\.com/${replace(var.cloudwatch_agent_ecr_repository, ".", "\\.")}@sha256:[0-9a-f]{64}$",
      var.cloudwatch_agent_image,
    ))
    error_message = "cloudwatch_agent_image must be a digest in the explicitly approved account, region, and agent ECR repository."
  }
}

variable "cloudwatch_agent_ecr_repository" {
  type        = string
  description = "Exact CloudWatch-agent ECR repository output by the shared bootstrap."

  validation {
    condition     = length(var.cloudwatch_agent_ecr_repository) <= 256 && can(regex("^elspeth-[a-z0-9]+((\\.|_|__|-+)[a-z0-9]+)*(/[a-z0-9]+((\\.|_|__|-+)[a-z0-9]+)*)*$", var.cloudwatch_agent_ecr_repository))
    error_message = "cloudwatch_agent_ecr_repository must be the bootstrap-created elspeth-prefixed AWS ECR repository name and be at most 256 characters."
  }
}

variable "composer_model" {
  type = string

  validation {
    condition     = !startswith(var.composer_model, "bedrock/") && var.composer_model != var.composer_advisor_model
    error_message = "composer_model must be a non-bedrock model alias (the gateway sidecar owns the upstream) distinct from composer_advisor_model."
  }
}

variable "composer_advisor_model" {
  type = string

  validation {
    condition     = !startswith(var.composer_advisor_model, "bedrock/")
    error_message = "composer_advisor_model must be a non-bedrock model alias (the gateway sidecar owns the upstream)."
  }
}




variable "alarm_actions" {
  type    = list(string)
  default = []
}

variable "alb_https_ingress_cidrs" {
  type        = list(string)
  description = "Explicit operator IPv4 CIDRs allowed to reach the acceptance ALB over HTTPS. IPv6 is not supported."

  validation {
    # Read the PARSED prefix, never the raw suffix. `!endswith(cidr, "/0")`
    # let "0.0.0.0/00" through, and EC2 canonicalised it straight back to
    # 0.0.0.0/0 — the fail-closed operator allowlist became a world-open
    # HTTPS rule. Uniqueness is checked on the canonical network for the same
    # reason: "10.0.0.5/8" and "10.0.0.6/8" build one rule, not two.
    condition = (
      length(var.alb_https_ingress_cidrs) > 0 &&
      alltrue([
        for cidr in var.alb_https_ingress_cidrs : try(cidrnetmask(cidr) != "0.0.0.0", false)
      ]) &&
      # A prefix floor, not just a /0 ban: a pair of /1 entries (or a
      # handful of /2..../7) reopens the world while every entry passes
      # the parseability check. /8 is far broader than any operator
      # egress allowlist legitimately needs.
      alltrue([
        for cidr in var.alb_https_ingress_cidrs : try(tonumber(split("/", cidr)[1]) >= 8, false)
      ]) &&
      length(distinct([
        for cidr in var.alb_https_ingress_cidrs : try(cidrsubnet(cidr, 0, 0), cidr)
      ])) == length(var.alb_https_ingress_cidrs)
    )
    error_message = "alb_https_ingress_cidrs must be a non-empty list of parseable IPv4 CIDRs (IPv6 is not supported), each covering a distinct network with a prefix of /8 or narrower, because /0 in any spelling — including unions of broader prefixes — opens the ALB to the whole internet."
  }
}

variable "database_name" {
  type    = string
  default = "elspeth"

  validation {
    condition     = can(regex("^[A-Za-z_][A-Za-z0-9_]{0,62}$", var.database_name))
    error_message = "database_name must be a safe PostgreSQL identifier of at most 63 characters."
  }
}

variable "session_database_name" {
  type    = string
  default = "elspeth_session"

  validation {
    condition     = can(regex("^[A-Za-z_][A-Za-z0-9_]{0,62}$", var.session_database_name))
    error_message = "session_database_name must be a safe PostgreSQL identifier of at most 63 characters."
  }
}

variable "landscape_database_name" {
  type    = string
  default = "elspeth_landscape"

  validation {
    condition = (
      can(regex("^[A-Za-z_][A-Za-z0-9_]{0,62}$", var.landscape_database_name)) &&
      var.landscape_database_name != var.session_database_name
    )
    error_message = "landscape_database_name must be a safe PostgreSQL identifier distinct from session_database_name."
  }
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
  type        = string
  default     = "16.13"
  nullable    = false
  description = "Pinned Aurora PostgreSQL engine version validated for this package."

  validation {
    condition     = var.aurora_engine_version == "16.13"
    error_message = "this package is currently validated only for Aurora PostgreSQL 16.13."
  }
}

# ── Operator-configurable web plugin policy (forwarded to modules/scenario) ──
#
# Thin pass-throughs: every type, default, and validation rule lives in
# modules/scenario/variables.tf so there is one source of truth. Leave any of
# these unset (null) to keep the module's shipped default.

variable "plugin_allowlist" {
  type        = list(string)
  default     = null
  description = "Kind-qualified plugin ids authorized on top of the web core. Null keeps the module default."
}

variable "plugin_preferences" {
  type        = map(list(string))
  default     = null
  description = "Ordered implementation choice per control capability. Null keeps the module default."
}

variable "plugin_control_modes" {
  type        = map(string)
  default     = null
  description = "'required' or 'recommend' per control capability. Null keeps the module default (both required)."
}

variable "llm_profiles" {
  type = map(object({
    model                 = string
    required_capabilities = optional(list(string))
  }))
  default     = null
  description = "Operator LLM profiles offered to web authors, keyed by alias. Null derives standard/fast from the Composer models. Profiles lower to gateway bindings (loopback endpoint, module-owned bearer ref); required_capabilities defaults to the module's standard set."
}

variable "default_llm_profile" {
  type        = string
  default     = null
  description = "Alias offered first to authors and used by the first-run tutorial. Null selects the module default."
}



variable "adopt_container_insights_log_group" {
  type        = bool
  default     = false
  description = "Set true only on a same-namespace redeploy retry after a prior destroy: ECS's service-linked role re-creates the Container Insights performance log group minutes after the cluster goes INACTIVE (its final flush), leaving an untagged orphan with the exact name Terraform wants to create, so the next apply's CreateLogGroup fails with ResourceAlreadyExistsException. True formally imports that orphan back into state instead of trying to create it. False (the default) leaves fresh accounts, where no orphan exists, unaffected."
}

# ── Gateway sidecar inputs (all required; this root is always custom_gateway) ─

variable "gateway_image" {
  type        = string
  description = "Digest-pinned ECR image for the elspeth-llm-gateway sidecar."
}

variable "gateway_sha" {
  type        = string
  description = "Expected org.opencontainers.image.revision label of the gateway image."

  validation {
    condition     = can(regex("^[0-9a-f]{40}$", var.gateway_sha))
    error_message = "gateway_sha must be a 40-hex commit SHA."
  }
}

variable "gateway_ecr_repository" {
  type        = string
  description = "ECR repository name the gateway image must come from."

  validation {
    condition     = length(var.gateway_ecr_repository) <= 256 && can(regex("^elspeth-[a-z0-9]+((\\.|_|__|-+)[a-z0-9]+)*(/[a-z0-9]+((\\.|_|__|-+)[a-z0-9]+)*)*$", var.gateway_ecr_repository))
    error_message = "gateway_ecr_repository must use the elspeth- prefix and AWS ECR repository-name grammar, and be at most 256 characters."
  }
}

variable "gateway_bearer_secret_arn" {
  type        = string
  description = "Secrets Manager ARN holding the static ELSPETH-to-gateway bearer (operator-created; Terraform never sees the value)."

  validation {
    condition     = can(regex("^arn:aws:secretsmanager:${var.aws_region}:${var.aws_account_id}:secret:[A-Za-z0-9/_+=.@-]{1,519}$", var.gateway_bearer_secret_arn))
    error_message = "gateway_bearer_secret_arn must be an exact Secrets Manager ARN in this package's aws partition, aws_region, and aws_account_id."
  }
}

variable "gateway_oauth_client_id_secret_arn" {
  type        = string
  description = "Secrets Manager ARN holding the OAuth client ID (gateway container only)."

  validation {
    condition     = can(regex("^arn:aws:secretsmanager:${var.aws_region}:${var.aws_account_id}:secret:[A-Za-z0-9/_+=.@-]{1,519}$", var.gateway_oauth_client_id_secret_arn))
    error_message = "gateway_oauth_client_id_secret_arn must be an exact Secrets Manager ARN in this package's aws partition, aws_region, and aws_account_id."
  }
}

variable "gateway_oauth_client_secret_secret_arn" {
  type        = string
  description = "Secrets Manager ARN holding the OAuth client secret (gateway container only)."

  validation {
    condition     = can(regex("^arn:aws:secretsmanager:${var.aws_region}:${var.aws_account_id}:secret:[A-Za-z0-9/_+=.@-]{1,519}$", var.gateway_oauth_client_secret_secret_arn))
    error_message = "gateway_oauth_client_secret_secret_arn must be an exact Secrets Manager ARN in this package's aws partition, aws_region, and aws_account_id."
  }
}

variable "gateway_adapter" {
  type        = string
  description = "Adapter name the gateway must load; readiness fails on mismatch."
}

variable "gateway_upstream_origin" {
  type        = string
  description = "HTTPS origin of the agency upstream the gateway invokes (documented egress destination)."
}

variable "gateway_oauth_token_url" {
  type        = string
  description = "HTTPS OAuth2 client-credentials token endpoint."
}

variable "gateway_model_mappings_json" {
  type        = string
  description = "Strict-JSON object of model alias => adapter-defined mapping object; must cover both composer models."
}
