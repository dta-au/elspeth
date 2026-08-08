locals {
  doctor_network_configuration = jsonencode({
    awsvpcConfiguration = {
      subnets        = aws_subnet.public[*].id
      securityGroups = [aws_security_group.task.id]
      assignPublicIp = "ENABLED"
    }
  })
  schema_init_doctor_overrides = jsonencode({
    containerOverrides = [{
      name    = local.doctor_name
      command = ["doctor", "aws-ecs", "--init-schema", "--json"]
    }]
  })
  runtime_doctor_overrides = jsonencode({
    containerOverrides = [{
      name    = local.doctor_name
      command = ["doctor", "aws-ecs", "--json"]
    }]
  })

  forward_actions = jsonencode([{
    Type           = "forward"
    TargetGroupArn = aws_lb_target_group.web.arn
    Order          = 1
    ForwardConfig = {
      TargetGroups = [{
        TargetGroupArn = aws_lb_target_group.web.arn
        Weight         = 1
      }]
      TargetGroupStickinessConfig = {
        Enabled = false
      }
    }
  }])

  disabled_actions = jsonencode([{
    Type  = "fixed-response"
    Order = 1
    FixedResponseConfig = {
      ContentType = "text/plain"
      MessageBody = "deployment disabled"
      StatusCode  = "503"
    }
  }])

  scenario_values = {
    DEPLOYMENT_MODE                                 = local.deployment_mode
    TARGET_PLATFORM                                 = var.target_platform
    AWS_REGION                                      = var.aws_region
    ECS_CLUSTER                                     = local.cluster_name
    ECS_SERVICE                                     = local.service_name
    WEB_CONTAINER_NAME                              = local.web_container_name
    ELSPETH_WEB__DATA_DIR                           = local.data_dir
    ELSPETH_WEB__PAYLOAD_STORE_PATH                 = local.payload_store_path
    ELSPETH_WEB__PLUGIN_ALLOWLIST                   = local.plugin_allowlist
    ELSPETH_WEB__PLUGIN_PREFERENCES                 = local.plugin_preferences
    ELSPETH_WEB__PLUGIN_CONTROL_MODES               = local.plugin_control_modes
    ELSPETH_WEB__LLM_PROFILES                       = local.llm_profiles
    ELSPETH_WEB__DEFAULT_LLM_PROFILE                = local.default_llm_profile
    ELSPETH_WEB__BEDROCK_GUARDRAIL_PROFILES         = local.guardrail_profiles
    ELSPETH_WEB__BEDROCK_GUARDRAIL_DEFAULT_PROFILES = local.guardrail_defaults
    ELSPETH_WEB__COMPOSER_MODEL                     = var.composer_model
    ELSPETH_WEB__COMPOSER_ADVISOR_MODEL             = var.composer_advisor_model
    ELSPETH_ACCEPTANCE_PLUGIN_POLICY_BINDING_SHA256 = local.plugin_policy_binding_sha256
    ELSPETH_BEDROCK_LIVE_TEST_MODEL                 = local.bedrock_litellm_model
    CLOUDWATCH_AGENT_IMAGE                          = var.cloudwatch_agent_image
    CLOUDWATCH_AGENT_CONFIG_JSON_SHA256             = local.cw_agent_config_json_sha256
    CLOUDWATCH_AGENT_OTEL_YAML_SHA256               = local.cw_agent_otel_yaml_sha256
    TARGET_GROUP_ARN                                = aws_lb_target_group.web.arn
    ALB_BASE_URL                                    = "https://${aws_lb.web.dns_name}"
    ALB_ARN                                         = aws_lb.web.arn
    CANDIDATE_TASK_DEFINITION                       = aws_ecs_task_definition.candidate_web.arn
    DOCTOR_TASK_DEFINITION                          = aws_ecs_task_definition.runtime_doctor.arn
    DOCTOR_CONTAINER_NAME                           = local.doctor_name
    DOCTOR_NETWORK_CONFIGURATION                    = local.doctor_network_configuration
    PAYLOAD_VERIFIER_TASK_DEFINITION                = aws_ecs_task_definition.payload.arn
    LOCAL_AUTH_VERIFIER_TASK_DEFINITION             = aws_ecs_task_definition.local_auth.arn
    ROLLBACK_DOCTOR_TASK_DEFINITION                 = local.deployment_mode == "upgrade" ? aws_ecs_task_definition.rollback_doctor[0].arn : ""
    WEB_LOG_GROUP                                   = local.web_log_group
    WEB_LOG_STREAM_PREFIX                           = "web"
    DOCTOR_LOG_GROUP                                = local.doctor_log_group
    DOCTOR_LOG_STREAM_PREFIX                        = "doctor"
    OPERATOR_METRICS_LOG_GROUP                      = local.operator_log_group
    ECS_DEPLOYMENT_EVENT_RULE                       = local.event_rule_name
    ECS_DEPLOYMENT_EVENT_TARGET_ID                  = local.event_target_id
    ECS_DEPLOYMENT_EVENT_LOG_GROUP                  = local.event_log_group
    PREVIOUS_TASK_DEFINITION                        = local.deployment_mode == "upgrade" ? aws_ecs_task_definition.rollback_web[0].arn : ""
    FIRST_DEPLOY_LISTENER_RULE_ARN                  = aws_lb_listener_rule.traffic.arn
    FIRST_DEPLOY_FORWARD_ACTIONS                    = local.forward_actions
    FIRST_DEPLOY_DISABLED_ACTIONS                   = local.disabled_actions
    COGNITO_USER_POOL_ID                            = local.deployment_mode == "upgrade" ? aws_cognito_user_pool.web[0].id : ""
    DB_CLUSTER_IDENTIFIER                           = local.database_identifier
    ELSPETH_TEST_S3_BUCKET                          = local.s3_bucket_name
    OIDC_EXPECTED_ISSUER                            = local.oidc_issuer
    OIDC_EXPECTED_AUDIENCE                          = local.deployment_mode == "upgrade" ? aws_cognito_user_pool_client.web[0].id : ""
    OIDC_EXPECTED_AUTHORIZATION_ORIGIN              = local.oidc_authorization_origin
    OIDC_EXPECTED_AUDIENCE_CLAIM                    = "client_id"
    SCENARIO_TF_DIR                                 = var.scenario_tf_dir
    SCENARIO_TF_VARS                                = var.scenario_tf_vars
    SCENARIO_TF_BINDING_SHA                         = var.scenario_tf_binding_sha
    SCENARIO_TF_BINDING_FILE                        = var.scenario_tf_binding_file
  }

  task_definition_families = concat(
    [
      local.web_family,
      local.schema_init_doctor_family,
      local.runtime_doctor_family,
      local.payload_family,
      local.local_auth_family,
      local.database_bootstrap_family,
    ],
    local.deployment_mode == "upgrade" ? [local.rollback_web_family, local.rollback_doctor_family] : [],
  )

  scenario_orphan_sweep = {
    tag_key                      = "ACCEPTANCE_RUN_ID"
    cleanup_owner                = var.owner
    ecs_task_definition_families = local.task_definition_families
    elbv2_listener_arns          = [aws_lb_listener.https.arn]
    rds_db_instance_identifiers  = [local.database_instance]
    efs_creation_tokens          = [local.efs_creation_token]
    efs_file_system_ids          = [aws_efs_file_system.data.id]
    efs_access_point_ids         = [aws_efs_access_point.data.id]
    secret_ids = [
      local.database_runtime_secret_name,
      local.database_schema_secret_name,
      local.database_bootstrap_secret_name,
    ]
    iam_role_names                     = [aws_iam_role.task.name, aws_iam_role.execution.name]
    log_group_names                    = [local.web_log_group, local.doctor_log_group, local.event_log_group, local.operator_log_group]
    log_resource_policy_names          = [local.log_policy_name]
    cloudwatch_dashboard_names         = [local.dashboard_name]
    cloudwatch_alarm_names             = local.alarm_names
    cloudwatch_retained_metrics        = []
    xray_group_names                   = [local.xray_group_name]
    xray_sampling_rule_names           = [local.xray_sampling_name]
    xray_retained_trace_ids            = []
    transaction_search_baseline_sha256 = var.transaction_search_baseline_sha256
    event_rules = [{
      event_bus_name = "default"
      rule_name      = local.event_rule_name
      target_ids     = [local.event_target_id]
    }]
    bedrock_guardrails = [
      {
        identifier = aws_bedrock_guardrail.prompt.guardrail_id
        versions   = [aws_bedrock_guardrail_version.prompt.version]
      },
      {
        identifier = aws_bedrock_guardrail.content.guardrail_id
        versions   = [aws_bedrock_guardrail_version.content.version]
      },
    ]
    cognito_subject_sub             = local.deployment_mode == "upgrade" ? var.cognito_subject_sub : ""
    cognito_pool_owned              = local.deployment_mode == "upgrade"
    expected_retained_metric_series = 0
    expected_retained_trace_ids     = 0
  }
}

output "resolved_inventory" {
  value = {
    schema            = "elspeth.aws-ecs-scenario-inventory.v7"
    acceptance_run_id = var.run_id
    candidate_sha     = var.candidate_sha
    aws_account_id    = var.aws_account_id
    aws_region        = var.aws_region
    scenario_id       = var.scenario_id
    phase             = "resolved"
    values            = local.scenario_values
    orphan_sweep      = local.scenario_orphan_sweep
  }
  sensitive = true
}

output "acceptance_tls_ca_pem" {
  description = "CA certificate for the disposable self-signed ALB certificate; it is valid for only 24 hours."
  value       = tls_self_signed_cert.alb.cert_pem
  sensitive   = true
}

output "public_url" {
  description = "Public HTTPS URL. Clients must trust acceptance_tls_ca_pem during the certificate's 24-hour lifetime."
  value       = "https://${aws_lb.web.dns_name}"
}

output "namespace" {
  description = "Run-scoped name prefix every resource of this scenario is built from (log groups, dashboard, X-Ray group, task families, alarms). Read it here; do not re-derive it from another name."
  value       = local.namespace
}

output "cluster_name" {
  value = aws_ecs_cluster.scenario.name
}

output "service_name" {
  value = aws_ecs_service.web.name
}

output "task_role_arn" {
  value = aws_iam_role.task.arn
}

output "runtime_database_secret_arn" {
  description = "Secrets Manager ARN containing separate session_url and landscape_url runtime connections."
  value       = aws_secretsmanager_secret.runtime.arn
}

output "schema_init_doctor_task_definition_arn" {
  description = "Schema-owner task definition for the first doctor aws-ecs --init-schema --json run only."
  value       = aws_ecs_task_definition.schema_init_doctor.arn
}

output "schema_init_doctor_network_configuration" {
  description = "AWS CLI network JSON for the schema-init doctor task."
  value       = local.doctor_network_configuration
}

output "schema_init_doctor_overrides" {
  description = "AWS CLI overrides JSON fixing the schema-init doctor command."
  value       = local.schema_init_doctor_overrides
}

output "runtime_doctor_task_definition_arn" {
  description = "Runtime-credential task definition for the ordinary doctor aws-ecs --json run."
  value       = aws_ecs_task_definition.runtime_doctor.arn
}

output "runtime_doctor_network_configuration" {
  description = "AWS CLI network JSON for the ordinary runtime doctor task."
  value       = local.doctor_network_configuration
}

output "runtime_doctor_overrides" {
  description = "AWS CLI overrides JSON fixing the ordinary runtime doctor command."
  value       = local.runtime_doctor_overrides
}

output "service_enable_command" {
  description = "Run only after the schema-init and ordinary runtime doctor tasks both exit zero."
  value       = "aws --profile ${jsonencode(var.aws_profile)} --region ${jsonencode(var.aws_region)} ecs update-service --cluster ${jsonencode(aws_ecs_cluster.scenario.name)} --service ${jsonencode(aws_ecs_service.web.name)} --task-definition ${jsonencode(aws_ecs_task_definition.candidate_web.arn)} --desired-count 1 --force-new-deployment"
}

output "teardown" {
  description = "Code-blind teardown reminder."
  value       = "Select the same backend config and workspace, verify account/region, then run terraform destroy."
}

output "effective_guardrail_policy" {
  description = "Guardrail content policies this scenario actually created. Surfaced so an operator can read the deployment's safety posture without decoding the module, and so the policy is assertable in tests."
  value = {
    prompt  = local.effective_prompt_guardrail
    content = local.effective_content_guardrail
  }
}
