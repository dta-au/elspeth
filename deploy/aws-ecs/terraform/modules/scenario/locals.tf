locals {
  scenario_id_upper = upper(var.scenario_id)
  scenario_id_lower = lower(var.scenario_id)
  namespace = format(
    "%s-%s",
    local.scenario_id_lower,
    substr(sha256("${lower(var.run_id)}\u0000${local.scenario_id_upper}"), 0, 20),
  )
  compact_run_id = replace(var.run_id, "-", "")
  name_prefix    = local.namespace

  tags = {
    ACCEPTANCE_RUN_ID = var.run_id
    Owner             = var.owner
    Purpose           = var.purpose
    RunId             = var.run_id
    CleanupDeadline   = var.cleanup_deadline
    Scenario          = local.scenario_id_upper
    ManagedBy         = "terraform"
  }

  cpu_architecture = var.target_platform == "linux/amd64" ? "X86_64" : "ARM64"
  deployment_mode  = var.scenario_id == "A" ? "first" : "upgrade"

  vpc_cidr            = var.scenario_id == "A" ? "10.71.0.0/16" : "10.72.0.0/16"
  public_subnet_cidrs = var.scenario_id == "A" ? ["10.71.0.0/20", "10.71.16.0/20"] : ["10.72.0.0/20", "10.72.16.0/20"]

  cluster_name           = "acceptance-${local.namespace}-cluster"
  service_name           = "acceptance-${local.namespace}-service"
  telemetry_service_name = "elspeth-web-${local.namespace}"
  web_container_name     = "elspeth-web"
  doctor_name            = "doctor"
  data_dir               = "/var/lib/elspeth"
  payload_store_path     = "/var/lib/elspeth/payloads"

  rds_ca_bundle_path = "/etc/elspeth/rds/global-bundle.pem"

  rds_ca_identifier = "rds-ca-rsa2048-g1"

  web_family                = "${local.namespace}-web"
  schema_init_doctor_family = "${local.namespace}-schema-init-doctor"
  runtime_doctor_family     = "${local.namespace}-runtime-doctor"
  payload_family            = "${local.namespace}-payload"
  local_auth_family         = "${local.namespace}-local-auth"
  rollback_web_family       = "${local.namespace}-rollback-web"
  rollback_doctor_family    = "${local.namespace}-rollback-doctor"
  database_bootstrap_family = "${local.namespace}-database-bootstrap"

  web_log_group      = "/aws/ecs/${local.namespace}-web"
  doctor_log_group   = "/aws/ecs/${local.namespace}-doctor"
  event_log_group    = "/aws/events/${local.namespace}-deployments"
  operator_log_group = "/aws/ecs/${local.namespace}-operator-metrics"
  log_policy_name    = "${local.namespace}-delivery-policy"
  event_rule_name    = "${local.namespace}-deployments"
  event_target_id    = "${local.namespace}-deployment-log"
  dashboard_name     = "${local.namespace}-elspeth-aws-operator-v1"

  alarm_names = [
    "${local.namespace}-RunFailureRate",
    "${local.namespace}-RunDurationP95",
    "${local.namespace}-ExternalCallFailureRate",
    "${local.namespace}-ExternalCallLatencyP95",
    "${local.namespace}-OperatorExportFailures",
    "${local.namespace}-OperatorExportStale",
    "${local.namespace}-OperatorSignalMissing",
  ]

  xray_group_name                = "${local.namespace}-xray"
  xray_sampling_name             = "${local.namespace}-sampling"
  database_identifier            = "${local.namespace}-aurora"
  database_instance              = "${local.namespace}-aurora-1"
  session_database               = var.session_database_name
  landscape_database             = var.landscape_database_name
  database_schema_role           = replace("${local.namespace}_schema", "-", "_")
  database_runtime_role          = replace("${local.namespace}_runtime", "-", "_")
  efs_creation_token             = "${local.namespace}-efs"
  database_runtime_secret_name   = "${local.namespace}-database-runtime"
  database_schema_secret_name    = "${local.namespace}-database-schema"
  database_bootstrap_secret_name = "${local.namespace}-database-bootstrap"
  s3_bucket_name                 = "elspeth-${local.namespace}-${substr(local.compact_run_id, 0, 12)}"
  s3_prefix                      = "${local.namespace}/${var.run_id}"

  plugin_allowlist = jsonencode([
    "source:aws_s3",
    "source:csv",
    "source:json",
    "source:text",
    "sink:aws_s3",
    "sink:csv",
    "sink:json",
    "sink:text",
    "transform:aws_bedrock_content_safety",
    "transform:aws_bedrock_prompt_shield",
    "transform:aws_textract_document_analysis",
    "transform:field_mapper",
    "transform:llm",
    "transform:passthrough",
    "transform:web_scrape",
  ])
  plugin_preferences = jsonencode({
    prompt_shield  = ["transform:aws_bedrock_prompt_shield"]
    content_safety = ["transform:aws_bedrock_content_safety"]
  })
  plugin_control_modes = jsonencode({
    prompt_shield  = "required"
    content_safety = "required"
  })
  # A cross-region ("global."/"us."/"eu."/"apac.") inference profile is authorized by Bedrock
  # against the underlying foundation model in whichever region it actually routes to, and that
  # authorization check reports a region-less resource ARN. A single region-pinned
  # foundation-model grant never matches that check, so derive an additional wildcard-region
  # grant for every configured Composer model (primary and advisor) that carries one of these
  # prefixes; a non-cross-region model keeps relying solely on the region-pinned ARNs supplied
  # in var.bedrock_foundation_model_arns.
  bedrock_cross_region_prefixes = ["global.", "us.", "eu.", "apac."]

  bedrock_configured_model_ids = distinct([
    trimprefix(var.composer_model, "bedrock/"),
    trimprefix(var.composer_advisor_model, "bedrock/"),
  ])

  # Map each configured model id to the cross-region prefix it starts with, or null if it is
  # region-pinned. try() turns the index-out-of-bounds error from an empty match list into null.
  bedrock_configured_model_cross_region_prefixes = {
    for model_id in local.bedrock_configured_model_ids : model_id => try(
      [for prefix in local.bedrock_cross_region_prefixes : prefix if startswith(model_id, prefix)][0],
      null,
    )
  }

  bedrock_cross_region_foundation_model_arns = distinct([
    for model_id, prefix in local.bedrock_configured_model_cross_region_prefixes :
    "arn:aws:bedrock:*::foundation-model/${trimprefix(model_id, prefix)}"
    if prefix != null
  ])

  bedrock_litellm_model      = var.composer_model
  bedrock_fast_litellm_model = var.composer_advisor_model
  # Two profiles so a pipeline can pick a tier, and web authors keep selecting an
  # opaque alias rather than a model id.
  #
  # Both models are taken from the configured Composer pair on purpose: IAM grants
  # bedrock:InvokeModel for exactly `bedrock_configured_model_ids`, which is derived
  # from composer_model and composer_advisor_model. A profile naming any other model
  # would validate at startup and then fail at invoke time with AccessDenied, so
  # adding a genuinely third model means extending that grant too.
  #
  # `fast` is named for the tier it provides, not the vendor's current model name —
  # the alias outlives a model swap, where "flash" or "glm47" would start lying the
  # first time composer_advisor_model changes.
  llm_profiles = jsonencode({
    tutorial = {
      provider    = "bedrock"
      model       = local.bedrock_litellm_model
      region_name = var.aws_region
    }
    fast = {
      provider    = "bedrock"
      model       = local.bedrock_fast_litellm_model
      region_name = var.aws_region
    }
  })
  # Stays the tutorial's profile AND the resolver's preferred alias, so adding
  # profiles never changes which one the planner teaches by default.
  tutorial_llm_profile = "tutorial"
  guardrail_profiles = jsonencode([
    {
      alias                = "prompt-approved"
      plugin               = "aws_bedrock_prompt_shield"
      guardrail_identifier = aws_bedrock_guardrail.prompt.guardrail_id
      guardrail_version    = aws_bedrock_guardrail_version.prompt.version
      region               = var.aws_region
    },
    {
      alias                = "content-approved"
      plugin               = "aws_bedrock_content_safety"
      guardrail_identifier = aws_bedrock_guardrail.content.guardrail_id
      guardrail_version    = aws_bedrock_guardrail_version.content.version
      region               = var.aws_region
    },
  ])
  guardrail_defaults = jsonencode({
    aws_bedrock_prompt_shield  = "prompt-approved"
    aws_bedrock_content_safety = "content-approved"
  })

  plugin_policy_projection = {
    ELSPETH_WEB__PLUGIN_ALLOWLIST                   = local.plugin_allowlist
    ELSPETH_WEB__PLUGIN_PREFERENCES                 = local.plugin_preferences
    ELSPETH_WEB__PLUGIN_CONTROL_MODES               = local.plugin_control_modes
    ELSPETH_WEB__LLM_PROFILES                       = local.llm_profiles
    ELSPETH_WEB__TUTORIAL_LLM_PROFILE               = local.tutorial_llm_profile
    ELSPETH_WEB__BEDROCK_GUARDRAIL_PROFILES         = local.guardrail_profiles
    ELSPETH_WEB__BEDROCK_GUARDRAIL_DEFAULT_PROFILES = local.guardrail_defaults
  }
  plugin_policy_binding_sha256 = sha256(jsonencode(local.plugin_policy_projection))

  policy_environment = [
    for name, value in merge(local.plugin_policy_projection, {
      ELSPETH_ACCEPTANCE_PLUGIN_POLICY_BINDING_SHA256 = local.plugin_policy_binding_sha256
      ELSPETH_BEDROCK_LIVE_TEST_MODEL                 = local.bedrock_litellm_model
      AWS_REGION                                      = var.aws_region
    }) : { name = name, value = value }
  ]

  runtime_environment = concat(local.policy_environment, [
    { name = "ELSPETH_WEB__HOST", value = "0.0.0.0" },
    { name = "ELSPETH_WEB__PORT", value = "8451" },
    { name = "ELSPETH_WEB__DEPLOYMENT_TARGET", value = "aws-ecs" },
    { name = "ELSPETH_WEB__DATA_DIR", value = local.data_dir },
    { name = "ELSPETH_WEB__PAYLOAD_STORE_PATH", value = local.payload_store_path },
    { name = "ELSPETH_WEB__COMPOSER_MAX_COMPOSITION_TURNS", value = "12" },
    { name = "ELSPETH_WEB__COMPOSER_MAX_DISCOVERY_TURNS", value = "8" },
    { name = "ELSPETH_WEB__COMPOSER_TIMEOUT_SECONDS", value = "120" },
    { name = "ELSPETH_WEB__COMPOSER_RATE_LIMIT_PER_MINUTE", value = "30" },
    { name = "ELSPETH_WEB__COMPOSER_BOOT_PROBE_ENABLED", value = "true" },
    { name = "ELSPETH_WEB__COMPOSER_MODEL", value = var.composer_model },
    { name = "ELSPETH_WEB__COMPOSER_ADVISOR_MODEL", value = var.composer_advisor_model },
    { name = "ELSPETH_WEB__REGISTRATION_MODE", value = "open" },
    { name = "ELSPETH_WEB__AUTH_PROVIDER", value = var.scenario_id == "A" ? "local" : "oidc" },
    { name = "ELSPETH_WEB__OPERATOR_TELEMETRY", value = "aws-otlp" },
    { name = "ELSPETH_WEB__OPERATOR_TELEMETRY_SERVICE_NAME", value = local.telemetry_service_name },
    { name = "ELSPETH_WEB__OPERATOR_TELEMETRY_ENVIRONMENT", value = "production" },
    { name = "ELSPETH_WEB__OPERATOR_TELEMETRY_RELEASE", value = var.candidate_sha },
    { name = "ELSPETH_WEB__OPERATOR_TELEMETRY_ECS_CLUSTER", value = local.cluster_name },
    { name = "ELSPETH_WEB__OPERATOR_TELEMETRY_ECS_SERVICE", value = local.service_name },
    { name = "ELSPETH_WEB__OPERATOR_TELEMETRY_TASK_DEFINITION_FAMILY", value = local.web_family },
    { name = "ELSPETH_WEB__OPERATOR_TELEMETRY_TASK_DEFINITION_REVISION", value = "1" },
    { name = "ELSPETH_ACCEPTANCE_RUN_ID", value = var.run_id },
    { name = "ELSPETH_ACCEPTANCE_CANDIDATE_SHA", value = var.candidate_sha },
    { name = "ELSPETH_ACCEPTANCE_SCENARIO_ID", value = var.scenario_id },
    { name = "ELSPETH_ACCEPTANCE_S3_BUCKET", value = local.s3_bucket_name },
    { name = "ELSPETH_ACCEPTANCE_S3_PREFIX", value = local.s3_prefix },
    ], var.scenario_id == "A" ? [] : [
    { name = "ELSPETH_WEB__OIDC_ISSUER", value = local.oidc_issuer },
    { name = "ELSPETH_WEB__OIDC_AUDIENCE", value = aws_cognito_user_pool_client.web[0].id },
    { name = "ELSPETH_WEB__OIDC_CLIENT_ID", value = aws_cognito_user_pool_client.web[0].id },
    { name = "ELSPETH_WEB__OIDC_AUTHORIZATION_ENDPOINT", value = "${local.oidc_authorization_origin}/oauth2/authorize" },
    { name = "ELSPETH_WEB__OIDC_TOKEN_ENDPOINT", value = "${local.oidc_authorization_origin}/oauth2/token" },
    { name = "ELSPETH_WEB__OIDC_AUTHORIZATION_ALLOWED_ORIGINS", value = jsonencode([local.oidc_authorization_origin]) },
    { name = "ELSPETH_WEB__OIDC_AUDIENCE_CLAIM", value = "client_id" },
  ])

  application_secrets = [
    { name = "ELSPETH_WEB__SECRET_KEY", valueFrom = "${aws_secretsmanager_secret.runtime.arn}:secret_key::" },
    { name = "ELSPETH_WEB__SHAREABLE_LINK_SIGNING_KEY", valueFrom = "${aws_secretsmanager_secret.runtime.arn}:shareable_link_signing_key::" },
  ]

  runtime_secrets = concat(local.application_secrets, [
    { name = "ELSPETH_WEB__SESSION_DB_URL", valueFrom = "${aws_secretsmanager_secret.runtime.arn}:session_url::" },
    { name = "ELSPETH_WEB__LANDSCAPE_URL", valueFrom = "${aws_secretsmanager_secret.runtime.arn}:landscape_url::" },
  ])

  schema_owner_secrets = concat(local.application_secrets, [
    { name = "ELSPETH_WEB__SESSION_DB_URL", valueFrom = "${aws_secretsmanager_secret.schema.arn}:session_url::" },
    { name = "ELSPETH_WEB__LANDSCAPE_URL", valueFrom = "${aws_secretsmanager_secret.schema.arn}:landscape_url::" },
  ])

  database_bootstrap_secrets = [
    { name = "ELSPETH_DB_ADMIN_URL", valueFrom = "${aws_secretsmanager_secret.bootstrap.arn}:admin_url::" },
    { name = "ELSPETH_DB_SCHEMA_PASSWORD", valueFrom = "${aws_secretsmanager_secret.bootstrap.arn}:schema_password::" },
    { name = "ELSPETH_DB_RUNTIME_PASSWORD", valueFrom = "${aws_secretsmanager_secret.bootstrap.arn}:runtime_password::" },
  ]

  cw_agent_json = file("${path.module}/../../telemetry/elspeth.cloudwatch-agent.v1/elspeth.cloudwatch-agent.v1.json")
  cw_agent_otel = replace(
    file("${path.module}/../../telemetry/elspeth.cloudwatch-agent.v1/elspeth.cloudwatch-agent.v1.otel.yaml"),
    "$${OPERATOR_METRICS_LOG_GROUP}",
    local.operator_log_group,
  )
  cw_agent_config_json_sha256 = sha256(local.cw_agent_json)
  cw_agent_otel_yaml_sha256   = sha256(local.cw_agent_otel)
  cw_agent_command            = "CONFIG_DIR=/tmp/elspeth-cloudwatch-agent; TRANSLATOR=/opt/aws/amazon-cloudwatch-agent/bin/config-translator; AGENT=/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent; mkdir -p \"$CONFIG_DIR\"; printf '%s' \"$ELSPETH_CW_AGENT_CONFIG_JSON_B64\" | base64 -d > \"/tmp/elspeth-cloudwatch-agent/elspeth.cloudwatch-agent.v1.json\"; printf '%s' \"$ELSPETH_CW_AGENT_OTEL_YAML_B64\" | base64 -d > \"/tmp/elspeth-cloudwatch-agent/elspeth.cloudwatch-agent.v1.otel.yaml\"; printf '%s\\n' \"$ELSPETH_CW_AGENT_CONFIG_JSON_SHA256  /tmp/elspeth-cloudwatch-agent/elspeth.cloudwatch-agent.v1.json\" | sha256sum -c -; printf '%s\\n' \"$ELSPETH_CW_AGENT_OTEL_YAML_SHA256  /tmp/elspeth-cloudwatch-agent/elspeth.cloudwatch-agent.v1.otel.yaml\" | sha256sum -c -; \"$TRANSLATOR\" -mode auto -os linux -input \"/tmp/elspeth-cloudwatch-agent/elspeth.cloudwatch-agent.v1.json\" -output \"/tmp/elspeth-cloudwatch-agent/amazon-cloudwatch-agent.toml\"; exec \"$AGENT\" -config \"/tmp/elspeth-cloudwatch-agent/amazon-cloudwatch-agent.toml\" -otelconfig \"/tmp/elspeth-cloudwatch-agent/elspeth.cloudwatch-agent.v1.otel.yaml\""

  oidc_domain_prefix        = local.namespace
  oidc_authorization_origin = var.scenario_id == "B" ? "https://${aws_cognito_user_pool_domain.web[0].domain}.auth.${var.aws_region}.amazoncognito.com" : ""
  oidc_issuer               = var.scenario_id == "B" ? "https://cognito-idp.${var.aws_region}.amazonaws.com/${aws_cognito_user_pool.web[0].id}" : ""
}
