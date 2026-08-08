locals {
  scenario_tf_binding_sha = sha256(jsonencode(jsondecode(file(var.scenario_tf_binding_file))))
}

module "scenario" {
  source = "../modules/scenario"

  providers = {
    aws               = aws
    aws.iam_lifecycle = aws.iam_lifecycle
  }

  run_id               = var.run_id
  scenario_id          = var.scenario_id
  deployment_lifecycle = "upgrade"
  llm_backend          = "bedrock"
  candidate_sha                      = var.candidate_sha
  candidate_image                    = var.candidate_image
  candidate_ecr_repository           = var.candidate_ecr_repository
  rollback_baseline_image            = var.rollback_baseline_image
  rollback_baseline_sha              = var.rollback_baseline_sha
  aws_account_id                     = var.aws_account_id
  aws_region                         = var.aws_region
  aws_profile                        = var.aws_profile
  iam_permissions_boundary_arn       = var.iam_permissions_boundary_arn
  target_platform                    = var.target_platform
  owner                              = var.owner
  purpose                            = var.purpose
  cleanup_deadline                   = var.cleanup_deadline
  cloudwatch_agent_image             = var.cloudwatch_agent_image
  cloudwatch_agent_ecr_repository    = var.cloudwatch_agent_ecr_repository
  composer_model                     = var.composer_model
  composer_advisor_model             = var.composer_advisor_model
  bedrock_inference_profile_arns     = var.bedrock_inference_profile_arns
  bedrock_foundation_model_arns      = var.bedrock_foundation_model_arns
  plugin_allowlist                   = var.plugin_allowlist
  plugin_preferences                 = var.plugin_preferences
  plugin_control_modes               = var.plugin_control_modes
  llm_profiles                       = var.llm_profiles
  default_llm_profile                = var.default_llm_profile
  prompt_guardrail                   = var.prompt_guardrail
  content_guardrail                  = var.content_guardrail
  scenario_tf_dir                    = var.scenario_tf_dir
  scenario_tf_vars                   = var.scenario_tf_vars
  scenario_tf_binding_sha            = local.scenario_tf_binding_sha
  scenario_tf_binding_file           = var.scenario_tf_binding_file
  transaction_search_baseline_sha256 = var.transaction_search_baseline_sha256
  cognito_subject_sub                = var.cognito_subject_sub
  alarm_actions                      = var.alarm_actions
  alb_https_ingress_cidrs            = var.alb_https_ingress_cidrs
  database_name                      = var.database_name
  session_database_name              = var.session_database_name
  landscape_database_name            = var.landscape_database_name
  aurora_engine_major_version        = var.aurora_engine_major_version
  aurora_engine_version              = var.aurora_engine_version
}

# R2-D3 (elspeth-a229c247a1): ECS's service-linked role re-creates the
# Container Insights performance log group minutes after the cluster goes
# INACTIVE (a final Container Insights flush), leaving an untagged, unmanaged
# orphan carrying the exact name this module wants to create. The next apply
# on the same namespace then fails CreateLogGroup with
# ResourceAlreadyExistsException. A depends_on ordering fix cannot help — the
# collision happens after destroy completes, not during create. Generate a
# replacement plan with -var=adopt_container_insights_log_group=true on the
# documented redeploy-retry path to formally import that orphan back into
# state instead of trying to create it. Never add the variable while applying
# an existing saved plan; the default (false) is a no-op so fresh accounts,
# where no orphan exists, are unaffected.
import {
  for_each = var.adopt_container_insights_log_group ? [1] : []
  to       = module.scenario.aws_cloudwatch_log_group.container_insights
  id       = "/aws/ecs/containerinsights/acceptance-${module.scenario.namespace}-cluster/performance"
}
