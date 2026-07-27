locals {
  scenario_tf_binding_sha = sha256(jsonencode(jsondecode(file(var.scenario_tf_binding_file))))
}

module "scenario" {
  source = "../modules/scenario"

  run_id                             = var.run_id
  scenario_id                        = var.scenario_id
  candidate_sha                      = var.candidate_sha
  candidate_image                    = var.candidate_image
  rollback_baseline_image            = var.rollback_baseline_image
  rollback_baseline_sha              = var.rollback_baseline_sha
  aws_account_id                     = var.aws_account_id
  aws_region                         = var.aws_region
  aws_profile                        = var.aws_profile
  target_platform                    = var.target_platform
  owner                              = var.owner
  purpose                            = var.purpose
  cleanup_deadline                   = var.cleanup_deadline
  cloudwatch_agent_image             = var.cloudwatch_agent_image
  composer_model                     = var.composer_model
  composer_advisor_model             = var.composer_advisor_model
  bedrock_inference_profile_arns     = var.bedrock_inference_profile_arns
  bedrock_foundation_model_arns      = var.bedrock_foundation_model_arns
  scenario_tf_dir                    = var.scenario_tf_dir
  scenario_tf_vars                   = var.scenario_tf_vars
  scenario_tf_binding_sha            = local.scenario_tf_binding_sha
  scenario_tf_binding_file           = var.scenario_tf_binding_file
  transaction_search_baseline_sha256 = var.transaction_search_baseline_sha256
  cognito_subject_sub                = var.cognito_subject_sub
  alarm_actions                      = var.alarm_actions
  database_name                      = var.database_name
  session_database_name              = var.session_database_name
  landscape_database_name            = var.landscape_database_name
  aurora_engine_major_version        = var.aurora_engine_major_version
  aurora_engine_version              = var.aurora_engine_version
}
