locals {
  codeblind_compatibility_file        = abspath("${path.root}/codeblind-compatibility.json")
  codeblind_compatibility             = jsondecode(file(local.codeblind_compatibility_file))
  codeblind_compatibility_sha256      = sha256(jsonencode(local.codeblind_compatibility))
  codeblind_transaction_search_sha256 = sha256(jsonencode(local.codeblind_compatibility.transaction_search_baseline))
}

module "scenario" {
  source = "../modules/scenario"

  providers = {
    aws               = aws
    aws.iam_lifecycle = aws.iam_lifecycle
  }

  run_id                             = var.run_id
  scenario_id                        = var.scenario_id
  candidate_sha                      = var.candidate_sha
  candidate_image                    = var.candidate_image
  candidate_ecr_repository           = var.candidate_ecr_repository
  rollback_baseline_image            = var.candidate_image
  rollback_baseline_sha              = var.candidate_sha
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
  scenario_tf_dir                    = abspath(path.root)
  scenario_tf_vars                   = abspath("${path.root}/../examples/scenario-a.tfvars.example")
  scenario_tf_binding_sha            = local.codeblind_compatibility_sha256
  scenario_tf_binding_file           = local.codeblind_compatibility_file
  transaction_search_baseline_sha256 = local.codeblind_transaction_search_sha256
  cognito_subject_sub                = var.cognito_subject_sub
  alarm_actions                      = var.alarm_actions
  database_name                      = var.database_name
  session_database_name              = var.session_database_name
  landscape_database_name            = var.landscape_database_name
  aurora_engine_major_version        = var.aurora_engine_major_version
  aurora_engine_version              = var.aurora_engine_version
}
