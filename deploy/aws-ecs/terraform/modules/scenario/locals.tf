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
  deployment_mode  = var.deployment_lifecycle
  bedrock_backend  = var.llm_backend == "bedrock"

  # Per-letter network identity. Each maintained scenario owns a distinct /16
  # so disposable stacks for different scenarios can coexist without overlap.
  scenario_network = {
    A = { vpc = "10.71.0.0/16", public_subnets = ["10.71.0.0/20", "10.71.16.0/20"] }
    B = { vpc = "10.72.0.0/16", public_subnets = ["10.72.0.0/20", "10.72.16.0/20"] }
  }
  vpc_cidr            = local.scenario_network[var.scenario_id].vpc
  public_subnet_cidrs = local.scenario_network[var.scenario_id].public_subnets

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

  web_log_group                = "/aws/ecs/${local.namespace}-web"
  doctor_log_group             = "/aws/ecs/${local.namespace}-doctor"
  event_log_group              = "/aws/events/${local.namespace}-deployments"
  operator_log_group           = "/aws/ecs/${local.namespace}-operator-metrics"
  container_insights_log_group = "/aws/ecs/containerinsights/${local.cluster_name}/performance"
  log_policy_name              = "${local.namespace}-delivery-policy"
  event_rule_name              = "${local.namespace}-deployments"
  event_target_id              = "${local.namespace}-deployment-log"
  dashboard_name               = "${local.namespace}-elspeth-aws-operator-v1"

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

  # Shipped defaults for the operator-configurable plugin policy. Each paired
  # var.* defaults to null, so leaving a variable unset reproduces exactly the
  # policy this module rendered when these values were hardcoded here.
  default_plugin_allowlist = [
    # source:aws_s3 authorizes S3 reads for THIS deployment's own runtime
    # (batch/CLI, local trained-operator sessions) only. The web authoring
    # surface bans author-controlled S3 reads categorically
    # (web_aws_s3_source_policy_error, src/elspeth/web/provider_config_policy.py)
    # and declines this authorization rather than offering it
    # (PluginUnavailableReason.WEB_SURFACE_PROHIBITED). Do not delete this
    # entry to "fix" a web-surface exposure that the code already refuses —
    # see the guard test
    # test_scenario_allowlist_keeps_the_s3_source_authorization_the_web_surface_declines
    # in tests/unit/deployment/test_aws_ecs_terraform_package.py.
    "source:aws_s3",
    "source:csv",
    "source:json",
    # source:llm carries the same posture as transform:llm: an LLM plugin the
    # deployment authorizes wherever it authorizes LLM use at all. It shipped
    # and was discoverable but authorized NOWHERE until 2026-08-07
    # (elspeth-ba6a8dff24), so no cold install could author an LLM source --
    # acceptance battery rounds 4 and 5 both misread that as the composer
    # "authoring around" the mechanism when it was simply unreachable. The web
    # surface was already built for it: required_controls.py budgets
    # shield/safety controls per llm_source_location, coverage.py derives its
    # protected output fields, planner_authoring_aids.py carries its rules.
    "source:llm",
    "source:text",
    "sink:aws_s3",
    "sink:csv",
    # sink:document is paired with sink:text and must be authorized wherever
    # text is. text refuses any value carrying CR or LF to hold its
    # one-row-one-record invariant, so document is the only sink that can
    # publish generated multiline text. Authorizing text alone reproduces the
    # state that produced elspeth-afdf55a17c: a composer asked to write a
    # generated announcement to a file has no correct sink to choose and
    # authors the refusing one, which discards the row and publishes nothing.
    "sink:document",
    "sink:json",
    "sink:text",
    "transform:aws_bedrock_content_safety",
    "transform:aws_bedrock_prompt_shield",
    "transform:aws_textract_document_analysis",
    "transform:field_mapper",
    # line_explode and report_assemble are the REMEDIES the sinks' own guidance
    # names: text says route multiline values through line_explode, document
    # says combine rows with report_assemble. A remedy a deployment does not
    # authorize is a remedy the Composer cannot take, which leaves the same
    # dead end that produced elspeth-afdf55a17c — a prohibition naming no
    # reachable alternative. They ride with the sinks that cite them.
    "transform:line_explode",
    "transform:llm",
    "transform:passthrough",
    "transform:report_assemble",
    "transform:web_scrape",
  ]
  default_plugin_preferences = {
    prompt_shield  = ["transform:aws_bedrock_prompt_shield"]
    content_safety = ["transform:aws_bedrock_content_safety"]
  }
  # Both controls default to "required": an uncovered LLM node is rejected
  # rather than merely flagged. Weakening either to "recommend" is an explicit
  # operator decision, never a side effect of setting something else.
  default_plugin_control_modes = {
    prompt_shield  = "required"
    content_safety = "required"
  }
  required_web_plugin_ids = toset([
    "source:csv",
    "source:json",
    "source:llm",
    "source:text",
    "sink:csv",
    "sink:document",
    "sink:json",
    "sink:text",
    "transform:field_mapper",
    "transform:line_explode",
    "transform:llm",
    "transform:report_assemble",
    "transform:web_scrape",
  ])

  effective_plugin_allowlist     = var.plugin_allowlist == null ? local.default_plugin_allowlist : var.plugin_allowlist
  effective_plugin_preferences   = var.plugin_preferences == null ? local.default_plugin_preferences : var.plugin_preferences
  effective_plugin_control_modes = var.plugin_control_modes == null ? local.default_plugin_control_modes : var.plugin_control_modes
  effective_authorized_plugin_ids = setunion(
    local.required_web_plugin_ids,
    toset(local.effective_plugin_allowlist),
  )

  plugin_allowlist     = jsonencode(local.effective_plugin_allowlist)
  plugin_preferences   = jsonencode(local.effective_plugin_preferences)
  plugin_control_modes = jsonencode(local.effective_plugin_control_modes)
  # A cross-region inference profile is authorized by Bedrock against the underlying foundation
  # model in whichever region it actually routes to, and that authorization check reports a
  # region-less resource ARN. A single region-pinned foundation-model grant never matches that
  # check, so derive an additional wildcard-region grant for every configured Composer model
  # (primary and advisor) that carries one of these prefixes; a non-cross-region model keeps
  # relying solely on the region-pinned ARNs supplied in var.bedrock_foundation_model_arns.
  #
  # This list is an explicit allowlist and MUST be extended as AWS ships new geography
  # prefixes. It deliberately is not a pattern match: a model id's leading dotted label is
  # ambiguous between a geography ("au.anthropic.claude-opus-5") and a provider
  # ("zai.glm-4.7-flash", whose version segment also contains a dot), so no reliable
  # structural rule separates the two. A missing geography prefix used to fail open-ended —
  # terraform plan validated cleanly, no wildcard grant was derived, and bedrock:InvokeModel
  # then denied intermittently depending on which destination region the profile routed to.
  # The bedrock_unclassified_model_ids gate below closes that hole: a model id whose leading
  # dotted label matches neither this list nor bedrock_known_provider_prefixes fails the plan
  # (see the precondition on aws_iam_role_policy.task). After changing a Composer model, still
  # confirm the rendered task-role policy carries an
  # "arn:aws:bedrock:*::foundation-model/<base-id>" grant for it.
  bedrock_cross_region_prefixes = ["global.", "us.", "eu.", "apac.", "au.", "jp.", "ca.", "br.", "in."]

  # Leading dotted labels that are Bedrock model providers, not geographies. A match here
  # classifies the model id as region-pinned, so it correctly receives no wildcard-region
  # grant and relies on the region-pinned ARNs in var.bedrock_foundation_model_arns. Extend
  # this list when AWS ships a new serverless provider; an unlisted label is refused at plan
  # time rather than guessed at. GovCloud ("us-gov.") is deliberately absent: every ARN this
  # module renders is in the "aws" partition, so a GovCloud profile must fail the gate rather
  # than silently receive a wrong-partition grant.
  bedrock_known_provider_prefixes = [
    "ai21.",
    "amazon.",
    "anthropic.",
    "cohere.",
    "deepseek.",
    "luma.",
    "meta.",
    "mistral.",
    "openai.",
    "qwen.",
    "stability.",
    "twelvelabs.",
    "writer.",
    "zai.",
  ]

  # The grant follows the configuration instead of only the Composer pair.
  # Deriving it from the Composer models alone meant a profile naming any third
  # model passed startup validation and then died at invoke with AccessDenied —
  # a trap the module comment and the reference docs both had to WARN about.
  # Including the profile models makes that failure unreachable.
  bedrock_configured_model_ids = local.bedrock_backend ? distinct(concat(
    [
      trimprefix(var.composer_model, "bedrock/"),
      trimprefix(var.composer_advisor_model, "bedrock/"),
    ],
    [for profile in values(local.effective_llm_profile_bindings) : trimprefix(profile.model, "bedrock/")],
  )) : []

  # Map each configured model id to the cross-region prefix it starts with, or null if it is
  # region-pinned. try() turns the index-out-of-bounds error from an empty match list into null.
  bedrock_configured_model_cross_region_prefixes = {
    for model_id in local.bedrock_configured_model_ids : model_id => try(
      [for prefix in local.bedrock_cross_region_prefixes : prefix if startswith(model_id, prefix)][0],
      null,
    )
  }

  # Configured model ids whose leading dotted label is neither a known geography prefix nor a
  # known provider label, and which no explicit var.bedrock_foundation_model_arns entry names
  # (matched against both the full id and the id with its leading label stripped, because an
  # unrecognised label could be either a provider or a geography). Anything left in this list
  # is unclassifiable: deriving no grant would reproduce the silent intermittent AccessDeny
  # this gate exists to prevent, so the precondition on aws_iam_role_policy.task rejects the
  # plan and tells the operator what to supply instead.
  bedrock_unclassified_model_ids = [
    for model_id in local.bedrock_configured_model_ids : model_id
    if strcontains(model_id, ".")
    && local.bedrock_configured_model_cross_region_prefixes[model_id] == null
    && !anytrue([
      for prefix in local.bedrock_known_provider_prefixes : startswith(model_id, prefix)
    ])
    && !anytrue([
      for arn in var.bedrock_foundation_model_arns :
      endswith(arn, "/${model_id}") || endswith(arn, "/${try(regex("^[^.]+\\.(.+)$", model_id)[0], model_id)}")
    ])
  ]

  bedrock_cross_region_foundation_model_arns = distinct([
    for model_id, prefix in local.bedrock_configured_model_cross_region_prefixes :
    "arn:aws:bedrock:*::foundation-model/${trimprefix(model_id, prefix)}"
    if prefix != null
  ])

  # Empty under custom_gateway: the composer models are not Bedrock ids there
  # and the Bedrock live-test env var must not name an uninvokable model.
  bedrock_litellm_model      = local.bedrock_backend ? var.composer_model : ""
  bedrock_fast_litellm_model = local.bedrock_backend ? var.composer_advisor_model : ""
  # Two default profiles so a pipeline can pick a tier, and web authors keep
  # selecting an opaque alias rather than a model id.
  #
  # The default pair reuses the configured Composer models because those are the
  # two models the deployment already knows it can invoke. Operators may replace
  # the set entirely via var.llm_profiles; the IAM grant is derived from whatever
  # ends up here (see bedrock_configured_model_ids), so a third model no longer
  # needs the grant extended by hand.
  #
  # `fast` is named for the tier it provides, not the vendor's current model name —
  # the alias outlives a model swap, where "flash" or "glm47" would start lying the
  # first time composer_advisor_model changes.
  default_llm_profile_bindings = {
    standard = {
      provider    = "bedrock"
      model       = local.bedrock_litellm_model
      region_name = var.aws_region
    }
    fast = {
      provider    = "bedrock"
      model       = local.bedrock_fast_litellm_model
      region_name = var.aws_region
    }
  }
  effective_llm_profile_bindings = var.llm_profiles == null ? local.default_llm_profile_bindings : {
    for alias, profile in var.llm_profiles : alias => {
      provider    = "bedrock"
      model       = profile.model
      region_name = coalesce(profile.region_name, var.aws_region)
    }
  }
  bedrock_profile_model_ids = local.bedrock_backend ? {
    for alias, profile in local.effective_llm_profile_bindings :
    alias => trimprefix(profile.model, "bedrock/")
  } : {}
  bedrock_profile_cross_region_prefixes = {
    for alias, model_id in local.bedrock_profile_model_ids :
    alias => local.bedrock_configured_model_cross_region_prefixes[model_id]
  }
  bedrock_profile_foundation_model_arns = local.bedrock_backend ? distinct([
    for alias, profile in local.effective_llm_profile_bindings :
    "arn:aws:bedrock:${profile.region_name}::foundation-model/${local.bedrock_profile_model_ids[alias]}"
    if local.bedrock_profile_cross_region_prefixes[alias] == null
  ]) : []
  bedrock_profile_inference_profile_arns = local.bedrock_backend ? distinct([
    for alias, profile in local.effective_llm_profile_bindings :
    "arn:aws:bedrock:${profile.region_name}:${var.aws_account_id}:inference-profile/${local.bedrock_profile_model_ids[alias]}"
    if local.bedrock_profile_cross_region_prefixes[alias] != null
  ]) : []
  bedrock_invoke_model_arns = distinct(concat(
    tolist(var.bedrock_inference_profile_arns),
    tolist(var.bedrock_foundation_model_arns),
    local.bedrock_profile_foundation_model_arns,
    local.bedrock_profile_inference_profile_arns,
    local.bedrock_cross_region_foundation_model_arns,
  ))
  llm_profiles = jsonencode(local.effective_llm_profile_bindings)
  # The tutorial needs A profile, not its OWN profile — it points at the ordinary
  # standard-tier one, the same way the systemd deployment points this at whichever
  # general profile it defines. An alias called "tutorial" made every non-tutorial
  # pipeline on the deployment cite a tutorial-shaped name in its audit trail.
  #
  # This alias is also the profile resolver's preferred_alias, so it sorts first and
  # is what the planner's exemplars teach by default; keep it pointed at the
  # general-purpose tier rather than a special-purpose one.
  #
  # A dangling alias here is a web STARTUP error, i.e. discovered after a service
  # roll. The check_default_llm_profile_is_configured precondition below moves
  # that failure to `terraform plan`.
  default_llm_profile = var.default_llm_profile == null ? "standard" : var.default_llm_profile
  # Default Guardrail content policies. The split is the point: the prompt
  # shield screens what goes INTO the model (input strength set, output NONE),
  # content safety screens what comes OUT (output strength set, input NONE).
  # Overriding either variable replaces its filter list wholesale.
  default_prompt_guardrail = {
    filters = [
      { type = "PROMPT_ATTACK", input_strength = "HIGH", output_strength = "NONE" },
    ]
    blocked_input_messaging   = "Input blocked by acceptance policy."
    blocked_outputs_messaging = "Output blocked by acceptance policy."
  }
  default_content_guardrail = {
    filters = [
      for category in ["HATE", "INSULTS", "MISCONDUCT", "SEXUAL", "VIOLENCE"] :
      { type = category, input_strength = "NONE", output_strength = "HIGH" }
    ]
    blocked_input_messaging   = "Input blocked by acceptance policy."
    blocked_outputs_messaging = "Output blocked by acceptance policy."
  }

  effective_prompt_guardrail  = var.prompt_guardrail == null ? local.default_prompt_guardrail : var.prompt_guardrail
  effective_content_guardrail = var.content_guardrail == null ? local.default_content_guardrail : var.content_guardrail

  guardrail_profiles = local.bedrock_backend ? jsonencode([
    {
      alias                = "prompt-approved"
      plugin               = "aws_bedrock_prompt_shield"
      guardrail_identifier = aws_bedrock_guardrail.prompt[0].guardrail_id
      guardrail_version    = aws_bedrock_guardrail_version.prompt[0].version
      region               = var.aws_region
    },
    {
      alias                = "content-approved"
      plugin               = "aws_bedrock_content_safety"
      guardrail_identifier = aws_bedrock_guardrail.content[0].guardrail_id
      guardrail_version    = aws_bedrock_guardrail_version.content[0].version
      region               = var.aws_region
    },
  ]) : jsonencode([])
  guardrail_defaults = local.bedrock_backend ? jsonencode({
    aws_bedrock_prompt_shield  = "prompt-approved"
    aws_bedrock_content_safety = "content-approved"
  }) : jsonencode({})

  plugin_policy_projection = {
    ELSPETH_WEB__PLUGIN_ALLOWLIST                   = local.plugin_allowlist
    ELSPETH_WEB__PLUGIN_PREFERENCES                 = local.plugin_preferences
    ELSPETH_WEB__PLUGIN_CONTROL_MODES               = local.plugin_control_modes
    ELSPETH_WEB__LLM_PROFILES                       = local.llm_profiles
    ELSPETH_WEB__DEFAULT_LLM_PROFILE                = local.default_llm_profile
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
    # The composer envelope is a coupled three-leg chain driven by
    # var.alb_idle_timeout_seconds: the ALB idle_timeout (network.tf), the
    # transport ceiling below (so the app's boot guard validates against the
    # real proxy limit, not the WebSettings default 300), and the wall clock
    # (var.composer_timeout_seconds, plan-time capped at ceiling - 30s
    # headroom). Defaults 900/840 (elspeth-09c91778f5): the prior 300/240
    # envelope could not fund the shipped corpus - battery round-5 g03's
    # first authoring call lands at t=413s and its compose settles at
    # ~490-514s. Wall history: 120 hardcoded, then 240 (elspeth-f159d2394b).
    { name = "ELSPETH_WEB__COMPOSER_TRANSPORT_IDLE_CEILING_SECONDS", value = tostring(var.alb_idle_timeout_seconds) },
    { name = "ELSPETH_WEB__COMPOSER_TIMEOUT_SECONDS", value = tostring(var.composer_timeout_seconds) },
    # "medium", not the code default "high": the candidate role is the other
    # half of the wall-clock budget above, and the value is measured rather
    # than preferred. At "high" the g08 acceptance graph returned a 422 at the
    # clock, its six calls summing to ~270s (45+50+123+29+17+6) with the worst
    # a 123s thinking tail; the budget was consumed by the chain, not by any
    # one call. At "medium" the same graph completed in ~200s with no
    # regression on g01 (184s vs 192s). See
    # docs/acceptance/2026-08-05-compose-cost-measurement.md addendum 1 and the
    # operator decision on elspeth-930a163c85. This pin was applied to the live
    # task definition as an env-only revision and was missing here until
    # elspeth-52af290183, so a cold install silently reverted to "high" — the
    # one combination no acceptance run has ever passed on.
    { name = "ELSPETH_WEB__COMPOSER_CANDIDATE_REASONING_EFFORT", value = "medium" },
    { name = "ELSPETH_WEB__COMPOSER_RATE_LIMIT_PER_MINUTE", value = "30" },
    { name = "ELSPETH_WEB__COMPOSER_BOOT_PROBE_ENABLED", value = "true" },
    # JSON log rendering (elspeth-cd98ea9d82 Tier 3): CloudWatch Logs
    # Insights auto-parses JSON, so `filter request_id = "..."` is a working
    # field query — the middleware binds request_id onto every in-request
    # line via structlog contextvars (Tier 2).
    { name = "ELSPETH_WEB__LOG_JSON", value = "true" },
    { name = "ELSPETH_WEB__COMPOSER_MODEL", value = var.composer_model },
    { name = "ELSPETH_WEB__COMPOSER_ADVISOR_MODEL", value = var.composer_advisor_model },
    { name = "ELSPETH_WEB__REGISTRATION_MODE", value = "open" },
    { name = "ELSPETH_WEB__AUTH_PROVIDER", value = local.deployment_mode == "first" ? "local" : "oidc" },
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
    ], local.deployment_mode == "first" ? [] : [
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
  oidc_authorization_origin = local.deployment_mode == "upgrade" ? "https://${aws_cognito_user_pool_domain.web[0].domain}.auth.${var.aws_region}.amazoncognito.com" : ""
  oidc_issuer               = local.deployment_mode == "upgrade" ? "https://cognito-idp.${var.aws_region}.amazonaws.com/${aws_cognito_user_pool.web[0].id}" : ""
}
