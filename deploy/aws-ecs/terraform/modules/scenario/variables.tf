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
    condition = can(regex(
      "^${var.aws_account_id}\\.dkr\\.ecr\\.${var.aws_region}\\.amazonaws\\.com/${replace(var.candidate_ecr_repository, ".", "\\.")}@sha256:[0-9a-f]{64}$",
      var.candidate_image,
    ))
    error_message = "candidate_image must be a digest in the explicitly approved account, region, and ECR repository."
  }
}

variable "candidate_ecr_repository" {
  type        = string
  description = "Exact application-image ECR repository created by the shared bootstrap."

  validation {
    condition     = can(regex("^elspeth-[a-z0-9][a-z0-9._/-]{1,254}$", var.candidate_ecr_repository))
    error_message = "candidate_ecr_repository must be the bootstrap-created elspeth-prefixed repository name."
  }
}

variable "rollback_baseline_image" {
  type = string

  validation {
    condition = can(regex(
      "^${var.aws_account_id}\\.dkr\\.ecr\\.${var.aws_region}\\.amazonaws\\.com/${replace(var.candidate_ecr_repository, ".", "\\.")}@sha256:[0-9a-f]{64}$",
      var.rollback_baseline_image,
    ))
    error_message = "rollback_baseline_image must be a digest in the explicitly approved account, region, and application ECR repository."
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
  type = string
}

variable "cloudwatch_agent_image" {
  type = string

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
  description = "Exact CloudWatch-agent ECR repository created by the shared bootstrap."

  validation {
    condition     = can(regex("^elspeth-[a-z0-9][a-z0-9._/-]{1,254}$", var.cloudwatch_agent_ecr_repository))
    error_message = "cloudwatch_agent_ecr_repository must be the bootstrap-created elspeth-prefixed repository name."
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
    # Empty is legal for Scenario B on a fresh apply: the subject can only
    # exist after this apply creates the user pool and the operator creates
    # a user in it (two-phase bind — apply, create user, re-apply with the
    # sub). The acceptance inventory check separately refuses to run
    # against a pool with no bound subject.
    condition = var.scenario_id == "A" ? var.cognito_subject_sub == "" : (
      var.cognito_subject_sub == "" || can(regex("^[\\x20-\\x7e]{1,128}$", var.cognito_subject_sub))
    )
    error_message = "cognito_subject_sub must be empty for Scenario A and, for Scenario B, either empty (pre-bind fresh apply) or a printable subject of at most 128 characters."
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
  type     = string
  default  = "16.13"
  nullable = false

  validation {
    condition     = var.aurora_engine_version == "16.13"
    error_message = "this package is currently validated only for Aurora PostgreSQL 16.13."
  }
}

# ── Operator-configurable web plugin policy ──────────────────────────────
#
# These seven settings are the web service's protected plugin-policy
# assignment. They were hardcoded in locals.tf, which made the deployment's
# safety posture, LLM offering, and plugin surface unchangeable without
# editing the module. Each variable below is nullable and defaults to null;
# locals.tf coalesces null to the shipped default, so the defaults live in
# exactly one place and an unset variable reproduces the previous behaviour
# byte for byte.

variable "plugin_allowlist" {
  type        = list(string)
  default     = null
  description = "Kind-qualified plugin ids authorized on top of the always-authorized web core. Null keeps the module default. An empty list authorizes only the core."

  validation {
    condition = var.plugin_allowlist == null || alltrue([
      for id in var.plugin_allowlist : can(regex("^(source|transform|sink):[a-z0-9_]+$", id))
    ])
    error_message = "each plugin_allowlist entry must be kind-qualified, e.g. \"transform:llm\"."
  }
}

variable "plugin_preferences" {
  type        = map(list(string))
  default     = null
  description = "Ordered implementation choice per control capability, e.g. {prompt_shield = [\"transform:aws_bedrock_prompt_shield\"]}. Null keeps the module default."

  validation {
    condition = var.plugin_preferences == null || alltrue([
      for capability in keys(var.plugin_preferences) : contains(["llm", "prompt_shield", "content_safety"], capability)
    ])
    error_message = "plugin_preferences keys must be one of \"llm\", \"prompt_shield\", or \"content_safety\"."
  }

  validation {
    condition = var.plugin_preferences == null || var.plugin_allowlist == null || alltrue(flatten([
      for implementations in values(var.plugin_preferences) : [
        for implementation in implementations :
        contains(
          setunion(
            toset(var.plugin_allowlist),
            # Hand-mirrors locals.tf's required_web_plugin_ids: a variable
            # validation cannot reference a local, so the always-authorized web
            # core has to be restated here. The two are pinned equal by
            # test_always_authorized_web_core_mirror_matches_the_locals_set.
            toset([
              "source:csv",
              "source:json",
              "source:llm",
              "source:text",
              "sink:csv",
              "sink:document",
              "sink:json",
              "sink:text",
              "transform:field_mapper",
              "transform:llm",
              "transform:web_scrape",
            ]),
          ),
          implementation,
        )
      ]
    ]))
    error_message = "every preferred implementation must be authorized by the always-authorized web core or plugin_allowlist."
  }
}

variable "plugin_control_modes" {
  type        = map(string)
  default     = null
  description = "Enforcement mode per control capability. 'required' rejects a web-authored pipeline whose LLM nodes are not covered; 'recommend' makes the control advisory. Null keeps the module default (both required)."

  validation {
    condition = var.plugin_control_modes == null || alltrue([
      for mode in values(var.plugin_control_modes) : contains(["required", "recommend"], mode)
    ])
    error_message = "each plugin_control_modes value must be \"required\" or \"recommend\"."
  }

  validation {
    condition = var.plugin_control_modes == null || alltrue([
      for capability in keys(var.plugin_control_modes) : contains(["llm", "prompt_shield", "content_safety"], capability)
    ])
    error_message = "plugin_control_modes keys must be one of \"llm\", \"prompt_shield\", or \"content_safety\"."
  }

  # The web service compiles the policy at startup and fails closed on both of
  # these, i.e. after the service is rolled. Checking here fails the plan.
  # Each guard is skipped when the paired variable is null, because the shipped
  # defaults in locals.tf are mutually consistent by construction; the resource
  # preconditions on aws_ecs_task_definition.candidate_web re-check every
  # combination, including the defaults.
  validation {
    condition = var.plugin_control_modes == null || var.plugin_preferences == null || alltrue([
      for capability, mode in var.plugin_control_modes :
      length(lookup(var.plugin_preferences, capability, [])) > 0
      if mode == "required"
    ])
    error_message = "a control set to \"required\" must name at least one implementation in plugin_preferences."
  }

}

variable "llm_profiles" {
  type = map(object({
    model       = string
    region_name = optional(string)
  }))
  default     = null
  description = "Operator LLM profiles offered to web authors, keyed by the opaque alias an author selects. Bedrock-only here, so profiles are keyless. Null derives the standard/fast pair from the Composer models. Every model named here is added to the task role's bedrock:InvokeModel grant. max_tokens is not exposed yet: emitting it would put an explicit null in the rendered policy for every profile that omits it."

  validation {
    condition = var.llm_profiles == null || alltrue([
      for profile in values(var.llm_profiles) : startswith(profile.model, "bedrock/")
    ])
    error_message = "each llm_profiles model must be a bedrock/... provider id; this module grants Bedrock access only."
  }

  validation {
    condition = var.llm_profiles == null || alltrue([
      for alias in keys(var.llm_profiles) : can(regex("^[a-z0-9]+([_-][a-z0-9]+)*$", alias))
    ])
    error_message = "each llm_profiles alias must be a lowercase identifier, words joined with - or _."
  }
}

variable "default_llm_profile" {
  type        = string
  default     = null
  description = "Alias listed first for authors, cited by the Composer's worked examples, and used by the first-run tutorial. Must name a configured profile. Null selects the module default (\"standard\")."

  # An alias that names no configured profile is a web STARTUP error, so without
  # this it is discovered only after the service is rolled. Checking it here
  # fails `terraform plan` instead.
  validation {
    condition = contains(
      var.llm_profiles == null ? ["standard", "fast"] : keys(var.llm_profiles),
      coalesce(var.default_llm_profile, "standard"),
    )
    error_message = "default_llm_profile must name a configured llm_profiles alias; the module default pair is \"standard\" and \"fast\"."
  }
}

variable "alb_idle_timeout_seconds" {
  type        = number
  default     = 900
  description = "ALB idle_timeout, and thereby the composer transport ceiling: locals.tf wires ELSPETH_WEB__COMPOSER_TRANSPORT_IDLE_CEILING_SECONDS to this value so the app's boot guard validates the wall clock against the real proxy limit. Default 900 (was 300, the AWS default) because the shipped corpus needs it: battery round-5 g03's compose settled at ~490-514s and could not reach its first authoring call inside the old 270s ceiling (elspeth-09c91778f5)."

  validation {
    condition     = var.alb_idle_timeout_seconds >= 60 && var.alb_idle_timeout_seconds <= 4000
    error_message = "alb_idle_timeout_seconds must be in [60, 4000]: 4000 is the ALB maximum, and below 60 the composer envelope cannot fund even a single authoring turn."
  }
}

variable "composer_timeout_seconds" {
  type        = number
  default     = 840
  description = "Composer whole-request wall clock in seconds. Default 840 (elspeth-09c91778f5: the shipped 240 could not fund the shipped corpus - g03's first authoring call lands at t=413s; 840 is the battery round-5 arm-B proven value, funding 56 turns at the app's 15s/turn planning floor). Was 240 (elspeth-f159d2394b), before that a hardcoded 120 that funded ~6 of the authorised 20 turns."

  # A wall clock above (transport idle ceiling - 30s headroom) is a web
  # STARTUP error, so without this check it is discovered only after the
  # service is rolled. Checking it here fails `terraform plan` instead. The
  # ceiling IS the ALB idle timeout (locals.tf wires the env var to
  # var.alb_idle_timeout_seconds), so the two legs cannot drift: raising the
  # wall past the proxy's patience is rejected at plan time. The 30 literal
  # mirrors the WebSettings composer_transport_headroom_seconds default.
  validation {
    condition     = var.composer_timeout_seconds > 0 && var.composer_timeout_seconds <= var.alb_idle_timeout_seconds - 30
    error_message = "composer_timeout_seconds must be in (0, alb_idle_timeout_seconds - 30]: the web app rejects a wall clock inside the transport headroom, because the ALB would abort with an opaque 504 before the composer reported its honest 422."
  }
}

variable "prompt_guardrail" {
  type = object({
    filters = list(object({
      type            = string
      input_strength  = string
      output_strength = string
    }))
    blocked_input_messaging   = optional(string, "Input blocked by acceptance policy.")
    blocked_outputs_messaging = optional(string, "Output blocked by acceptance policy.")
  })
  default     = null
  description = "Bedrock Guardrail content policy for the prompt shield, which screens what goes INTO the model. Null keeps the module default (one PROMPT_ATTACK filter at input HIGH). Only content filters are configurable here; denied-topic, PII, word, and contextual-grounding policies are not yet exposed."

  validation {
    condition = var.prompt_guardrail == null || alltrue(flatten([
      for filter in var.prompt_guardrail.filters : [
        contains(["NONE", "LOW", "MEDIUM", "HIGH"], filter.input_strength),
        contains(["NONE", "LOW", "MEDIUM", "HIGH"], filter.output_strength),
      ]
    ]))
    error_message = "prompt_guardrail filter strengths must each be NONE, LOW, MEDIUM, or HIGH."
  }

  validation {
    condition     = var.prompt_guardrail == null || length(var.prompt_guardrail.filters) > 0
    error_message = "prompt_guardrail.filters must not be empty; a Guardrail with no filter screens nothing."
  }
}

variable "content_guardrail" {
  type = object({
    filters = list(object({
      type            = string
      input_strength  = string
      output_strength = string
    }))
    blocked_input_messaging   = optional(string, "Input blocked by acceptance policy.")
    blocked_outputs_messaging = optional(string, "Output blocked by acceptance policy.")
  })
  default     = null
  description = "Bedrock Guardrail content policy for content safety, which screens what comes OUT of the model. Null keeps the module default (HATE, INSULTS, MISCONDUCT, SEXUAL, VIOLENCE at output HIGH). Same configurability limits as prompt_guardrail."

  validation {
    condition = var.content_guardrail == null || alltrue(flatten([
      for filter in var.content_guardrail.filters : [
        contains(["NONE", "LOW", "MEDIUM", "HIGH"], filter.input_strength),
        contains(["NONE", "LOW", "MEDIUM", "HIGH"], filter.output_strength),
      ]
    ]))
    error_message = "content_guardrail filter strengths must each be NONE, LOW, MEDIUM, or HIGH."
  }

  validation {
    condition     = var.content_guardrail == null || length(var.content_guardrail.filters) > 0
    error_message = "content_guardrail.filters must not be empty; a Guardrail with no filter screens nothing."
  }
}
