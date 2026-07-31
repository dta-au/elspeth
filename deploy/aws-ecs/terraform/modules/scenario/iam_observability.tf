data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "task" {
  name                 = "${local.namespace}-task-role"
  path                 = "/elspeth/${var.run_id}/"
  provider             = aws.iam_lifecycle
  assume_role_policy   = data.aws_iam_policy_document.ecs_tasks_assume.json
  permissions_boundary = var.iam_permissions_boundary_arn

  tags = local.tags
}

resource "aws_iam_role" "execution" {
  name                 = "${local.namespace}-execution-role"
  path                 = "/elspeth/${var.run_id}/"
  provider             = aws.iam_lifecycle
  assume_role_policy   = data.aws_iam_policy_document.ecs_tasks_assume.json
  permissions_boundary = var.iam_permissions_boundary_arn

  tags = local.tags
}

resource "aws_iam_role_policy_attachment" "execution_managed" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "execution_secrets" {
  statement {
    sid     = "ReadScenarioSecrets"
    actions = ["secretsmanager:GetSecretValue"]
    resources = [
      aws_secretsmanager_secret.runtime.arn,
      aws_secretsmanager_secret.schema.arn,
      aws_secretsmanager_secret.bootstrap.arn,
    ]
  }
}

resource "aws_iam_role_policy" "execution_secrets" {
  name   = "${local.namespace}-execution-secrets"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.execution_secrets.json
}

data "aws_iam_policy_document" "task" {
  statement {
    sid = "UseAcceptanceObjects"
    actions = [
      "s3:GetObject",
      "s3:GetObjectVersion",
      "s3:PutObject",
      "s3:DeleteObject",
    ]
    resources = ["${aws_s3_bucket.acceptance.arn}/${local.s3_prefix}/*"]
  }

  statement {
    # Unconditioned on purpose: S3 decides HeadObject/GetObject's missing-vs-forbidden response
    # (404 vs 403) with an implicit ListBucket check that runs outside the triggering request's
    # own context, so an s3:prefix condition here never matches and the object 403 persists. The
    # statement stays narrowly scoped because it names only this run's own disposable bucket, not
    # a wildcard bucket pattern.
    sid       = "ListAcceptanceBucket"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.acceptance.arn]
  }

  statement {
    sid       = "InvokeConfiguredBedrockModels"
    actions   = ["bedrock:InvokeModel"]
    resources = local.bedrock_invoke_model_arns
  }

  statement {
    sid       = "ApplyAcceptanceGuardrails"
    actions   = ["bedrock:ApplyGuardrail", "bedrock:GetGuardrail"]
    resources = [aws_bedrock_guardrail.prompt.guardrail_arn, aws_bedrock_guardrail.content.guardrail_arn]
  }

  statement {
    # Neither Textract async action names an ARN, so "*" is the only expressible resource
    # (the permissions boundary carries the same statement). Scope comes from the object
    # grant above: StartDocumentAnalysis reads DocumentLocation.S3Object under this role's
    # own credentials, so it can only analyse documents already inside this run's prefix.
    sid       = "RunDocumentAnalysis"
    actions   = ["textract:StartDocumentAnalysis", "textract:GetDocumentAnalysis"]
    resources = ["*"]
  }

  statement {
    sid = "MountAcceptanceEFS"
    actions = [
      "elasticfilesystem:ClientMount",
      "elasticfilesystem:ClientWrite",
    ]
    resources = [aws_efs_file_system.data.arn]

    condition {
      test     = "StringEquals"
      variable = "elasticfilesystem:AccessPointArn"
      values   = [aws_efs_access_point.data.arn]
    }
  }

  statement {
    sid = "ExecuteCommandChannels"
    actions = [
      "ssmmessages:CreateControlChannel",
      "ssmmessages:CreateDataChannel",
      "ssmmessages:OpenControlChannel",
      "ssmmessages:OpenDataChannel",
    ]
    resources = ["*"]
  }

  statement {
    sid = "PutElspethOperatorMetricEmf"
    actions = [
      "logs:DescribeLogStreams",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = [
      aws_cloudwatch_log_group.operator.arn,
      "${aws_cloudwatch_log_group.operator.arn}:log-stream:*",
    ]
  }

  statement {
    sid       = "PutElspethTraces"
    actions   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords"]
    resources = ["*"]
  }

  statement {
    sid       = "ReadElspethAcceptanceTelemetry"
    actions   = ["cloudwatch:GetMetricData", "xray:BatchGetTraces"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "task" {
  name   = "${local.namespace}-task-policy"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task.json

  # The wildcard-region Bedrock grant derivation in locals.tf classifies every configured
  # model id by its leading dotted label. An unrecognised label used to fail open: no grant
  # was derived, `terraform plan` validated cleanly, and bedrock:InvokeModel then denied
  # intermittently at runtime depending on which destination region a geography profile
  # routed to. Fail the plan instead and say exactly how to resolve it.
  lifecycle {
    precondition {
      condition = length(local.bedrock_unclassified_model_ids) == 0
      error_message = format(
        "Bedrock model id(s) [%s] carry a leading dotted label that is neither a known cross-region geography prefix (%s) nor a known provider label (%s), so the module cannot decide whether a wildcard-region foundation-model grant is required. If the label is a new AWS geography, add it to bedrock_cross_region_prefixes in modules/scenario/locals.tf; if it is a new provider, add it to bedrock_known_provider_prefixes there. Alternatively name the model explicitly in bedrock_foundation_model_arns: \"arn:aws:bedrock:*::foundation-model/<id-without-geography-prefix>\" for a cross-region geography profile, or the region-pinned foundation-model ARN for a provider model.",
        join(", ", local.bedrock_unclassified_model_ids),
        join(" ", local.bedrock_cross_region_prefixes),
        join(" ", local.bedrock_known_provider_prefixes),
      )
    }
  }
}

resource "aws_cloudwatch_log_group" "web" {
  name              = local.web_log_group
  retention_in_days = 30
  tags              = local.tags
}

resource "aws_cloudwatch_log_group" "doctor" {
  name              = local.doctor_log_group
  retention_in_days = 30
  tags              = local.tags
}

resource "aws_cloudwatch_log_group" "event" {
  name              = local.event_log_group
  retention_in_days = 7
  tags              = local.tags
}

resource "aws_cloudwatch_log_group" "operator" {
  name              = local.operator_log_group
  retention_in_days = 30
  tags              = local.tags
}

resource "aws_cloudwatch_log_group" "container_insights" {
  name              = local.container_insights_log_group
  retention_in_days = 1
  tags              = local.tags
}

data "aws_iam_policy_document" "event_log_delivery" {
  statement {
    sid = "EventBridgeToScenarioLog"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["${aws_cloudwatch_log_group.event.arn}:*"]

    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com", "delivery.logs.amazonaws.com"]
    }
  }
}

resource "aws_cloudwatch_log_resource_policy" "event" {
  policy_name     = local.log_policy_name
  policy_document = data.aws_iam_policy_document.event_log_delivery.json
}

resource "aws_cloudwatch_event_rule" "deployments" {
  name = local.event_rule_name
  event_pattern = jsonencode({
    source      = ["aws.ecs"]
    detail-type = ["ECS Deployment State Change"]
    detail = {
      eventName = ["SERVICE_DEPLOYMENT_FAILED"]
    }
  })

  tags = local.tags
}

resource "aws_cloudwatch_event_target" "deployment_log" {
  rule      = aws_cloudwatch_event_rule.deployments.name
  target_id = local.event_target_id
  arn       = aws_cloudwatch_log_group.event.arn

  depends_on = [aws_cloudwatch_log_resource_policy.event]
}

resource "aws_xray_group" "scenario" {
  group_name        = local.xray_group_name
  filter_expression = "service(\"${local.telemetry_service_name}\")"

  tags = local.tags
}

resource "aws_xray_sampling_rule" "scenario" {
  rule_name      = local.xray_sampling_name
  priority       = 1000
  version        = 1
  reservoir_size = 1
  fixed_rate     = 1
  url_path       = "*"
  host           = "*"
  http_method    = "*"
  service_type   = "*"
  service_name   = local.telemetry_service_name
  resource_arn   = "*"

  tags = local.tags
}

locals {
  cloudwatch_dimension_keys = [
    "service.name",
    "deployment.environment",
    "service.version",
    "cloud.provider",
    "aws.ecs.cluster.name",
    "aws.ecs.service.name",
    "aws.ecs.task.family",
    "aws.ecs.task.revision",
  ]

  candidate_cloudwatch_dimension_map = {
    "service.name"           = local.telemetry_service_name
    "deployment.environment" = "production"
    "service.version"        = var.candidate_sha
    "cloud.provider"         = "aws"
    "aws.ecs.cluster.name"   = local.cluster_name
    "aws.ecs.service.name"   = local.service_name
    "aws.ecs.task.family"    = local.web_family
    "aws.ecs.task.revision"  = tostring(aws_ecs_task_definition.candidate_web.revision)
  }

  rollback_cloudwatch_dimension_map = var.scenario_id == "B" ? {
    "service.name"           = local.telemetry_service_name
    "deployment.environment" = "production"
    "service.version"        = var.rollback_baseline_sha
    "cloud.provider"         = "aws"
    "aws.ecs.cluster.name"   = local.cluster_name
    "aws.ecs.service.name"   = local.service_name
    "aws.ecs.task.family"    = local.rollback_web_family
    "aws.ecs.task.revision"  = tostring(aws_ecs_task_definition.rollback_web[0].revision)
  } : null

  cloudwatch_dimension_maps = concat(
    [local.candidate_cloudwatch_dimension_map],
    var.scenario_id == "B" ? [local.rollback_cloudwatch_dimension_map] : [],
  )
  cloudwatch_dimension_maps_by_id = {
    for index, dimension_map in local.cloudwatch_dimension_maps :
    "identity_${index}" => dimension_map
  }
  cloudwatch_dimension_lists = [
    for dimension_map in local.cloudwatch_dimension_maps : flatten([
      for key in local.cloudwatch_dimension_keys : [key, dimension_map[key]]
    ])
  ]

  composer_failure_sources = [
    "compose",
    "recompose",
    "convergence",
    "plugin_crash",
    "runtime_preflight",
    "yaml_export",
    "state_seed",
    "cached_preflight",
  ]
  composer_failure_metric_specs = concat(
    flatten([
      for name in [
        "composer.runtime_preflight.total",
        "composer.authoring_validation.total",
        ] : [
        for source in local.composer_failure_sources : [
          for result in ["failed", "exception"] : {
            name = name
            dimensions = {
              result = result
              source = source
            }
          }
        ]
      ]
    ]),
    [
      {
        name = "composer.runtime_preflight.total"
        dimensions = {
          outcome = "failure"
        }
      },
      {
        name = "composer.authoring_validation.total"
        dimensions = {
          outcome = "invalid"
        }
      },
      {
        name       = "composer.audit.fetch_failure_total"
        dimensions = {}
      },
    ],
  )

  metric_widgets = [
    {
      title = "Run failures and duration"
      metrics = [
        { name = "run.failure", dimensions = {} },
        { name = "run.duration", dimensions = {} },
      ]
      stat = "Sum"
    },
    {
      title = "External-call failures and latency"
      metrics = [
        { name = "external_call.failure", dimensions = {} },
        { name = "external_call.latency", dimensions = {} },
      ]
      stat = "Sum"
    },
    {
      title = "LLM token totals"
      metrics = [
        { name = "llm.prompt_tokens", dimensions = {} },
        { name = "llm.completion_tokens", dimensions = {} },
      ]
      stat = "Sum"
    },
    {
      title   = "Composer and runtime failures"
      metrics = local.composer_failure_metric_specs
      stat    = "Sum"
    },
    {
      title = "Operator delivery health"
      metrics = [
        { name = "operator.telemetry.export_failures", dimensions = {} },
        { name = "operator.telemetry.queue_drops", dimensions = {} },
        { name = "operator.telemetry.last_success_age_seconds", dimensions = {} },
        { name = "operator.telemetry.collector_unavailable", dimensions = {} },
      ]
      stat = "Maximum"
    },
  ]

  direct_alarm_specs = {
    RunFailureRate = {
      metric_name        = "run.failure"
      statistic          = "Sum"
      extended_statistic = null
      period             = 300
      evaluation_periods = 1
      threshold          = 0
      treat_missing_data = "notBreaching"
      owner_action       = "Runtime owner queries Landscape terminal runs for this five-minute window."
    }
    RunDurationP95 = {
      metric_name        = "run.duration"
      statistic          = null
      extended_statistic = "p95"
      period             = 300
      evaluation_periods = 3
      threshold          = 300
      treat_missing_data = "notBreaching"
      owner_action       = "Runtime owner compares audited run timestamps and inspects slow nodes and calls."
    }
    ExternalCallFailureRate = {
      metric_name        = "external_call.failure"
      statistic          = "Sum"
      extended_statistic = null
      period             = 300
      evaluation_periods = 1
      threshold          = 0
      treat_missing_data = "notBreaching"
      owner_action       = "Plugin owner queries audited external calls by provider and operation."
    }
    ExternalCallLatencyP95 = {
      metric_name        = "external_call.latency"
      statistic          = null
      extended_statistic = "p95"
      period             = 300
      evaluation_periods = 3
      threshold          = 30
      treat_missing_data = "notBreaching"
      owner_action       = "Plugin owner compares audited call latency and provider status."
    }
    OperatorExportStale = {
      metric_name        = "operator.telemetry.last_success_age_seconds"
      statistic          = "Maximum"
      extended_statistic = null
      period             = 60
      evaluation_periods = 3
      threshold          = 180
      treat_missing_data = "breaching"
      owner_action       = "Platform owner checks sidecar health and confirms current Landscape writes."
    }
    OperatorSignalMissing = {
      metric_name        = "operator.telemetry.collector_unavailable"
      statistic          = "Maximum"
      extended_statistic = null
      period             = 60
      evaluation_periods = 3
      threshold          = 0
      treat_missing_data = "breaching"
      owner_action       = "On-call checks task health and collector logs, then queries Landscape for the blind window."
    }
  }
}

resource "aws_sns_topic" "operator_alarms" {
  name = "${local.namespace}-operator-alarms"
  tags = local.tags
}

resource "aws_cloudwatch_dashboard" "operator" {
  dashboard_name = local.dashboard_name
  dashboard_body = jsonencode({
    widgets = concat(
      [{
        type   = "text"
        x      = 0
        y      = 0
        width  = 24
        height = 2
        properties = {
          markdown = "# elspeth-aws-operator-v1 — ${local.namespace}"
        }
      }],
      [for index, widget in local.metric_widgets : {
        type   = "metric"
        x      = index % 2 == 0 ? 0 : 12
        y      = 2 + floor(index / 2) * 7
        width  = 12
        height = 7
        properties = {
          title  = widget.title
          region = var.aws_region
          stat   = widget.stat
          period = 300
          # CloudWatch requires an array OF ARRAYS of strings, one array per
          # metric row. flatten() is recursive and would collapse every row
          # into a single flat string list, which CreateDashboard rejects with
          # one "Should be array" error per element. The spread joins the
          # per-identity groups one level only, so each row stays an array.
          metrics = concat([
            for identity_dimensions in local.cloudwatch_dimension_lists : [
              for metric in widget.metrics : concat(
                ["ELSPETH/Operator", metric.name],
                identity_dimensions,
                flatten([
                  for key in sort(keys(metric.dimensions)) : [
                    key,
                    metric.dimensions[key],
                  ]
                ]),
              )
            ]
          ]...)
        }
      }],
    )
  })
}

resource "aws_cloudwatch_metric_alarm" "operator_direct" {
  for_each = local.direct_alarm_specs

  alarm_name          = "${local.namespace}-${each.key}"
  alarm_description   = each.value.owner_action
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = each.value.evaluation_periods
  threshold           = each.value.threshold
  treat_missing_data  = each.value.treat_missing_data
  alarm_actions       = concat([aws_sns_topic.operator_alarms.arn], var.alarm_actions)

  metric_query {
    id          = "active_identity"
    expression  = "MAX(METRICS())"
    label       = each.key
    return_data = true
  }

  dynamic "metric_query" {
    for_each = local.cloudwatch_dimension_maps_by_id

    content {
      id          = metric_query.key
      return_data = false

      metric {
        namespace   = "ELSPETH/Operator"
        metric_name = each.value.metric_name
        period      = each.value.period
        stat        = coalesce(each.value.extended_statistic, each.value.statistic)
        dimensions  = metric_query.value
      }
    }
  }

  tags = local.tags
}

resource "aws_cloudwatch_metric_alarm" "operator_export_failures" {
  alarm_name          = "${local.namespace}-OperatorExportFailures"
  alarm_description   = "Platform owner checks sidecar health, task-role delivery, and Landscape continuity."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  threshold           = 0
  treat_missing_data  = "notBreaching"
  alarm_actions       = concat([aws_sns_topic.operator_alarms.arn], var.alarm_actions)

  # The failure and drop counters are cumulative per task, so a replacement
  # task restarts them near zero. Summing identities before DIFF folds that
  # reset into one negative delta that the clamp discards along with the new
  # task's first real failures; each series is therefore diffed and clamped
  # on its own before the sum. CloudWatch allows at most 10 metric queries
  # per alarm and this shape uses 4N + 1 for N identities (N is at most 2).
  metric_query {
    id = "combined"
    expression = format(
      "SUM([%s])",
      join(", ", concat(
        [for id in keys(local.cloudwatch_dimension_maps_by_id) : "delta_failures_${id}"],
        [for id in keys(local.cloudwatch_dimension_maps_by_id) : "delta_drops_${id}"],
      )),
    )
    label       = "New export failures or queue drops"
    return_data = true
  }

  dynamic "metric_query" {
    for_each = local.cloudwatch_dimension_maps_by_id

    content {
      id          = "delta_failures_${metric_query.key}"
      expression  = "IF(DIFF(failures_${metric_query.key}) > 0, DIFF(failures_${metric_query.key}), 0)"
      return_data = false
    }
  }

  dynamic "metric_query" {
    for_each = local.cloudwatch_dimension_maps_by_id

    content {
      id          = "delta_drops_${metric_query.key}"
      expression  = "IF(DIFF(drops_${metric_query.key}) > 0, DIFF(drops_${metric_query.key}), 0)"
      return_data = false
    }
  }

  dynamic "metric_query" {
    for_each = local.cloudwatch_dimension_maps_by_id

    content {
      id          = "failures_${metric_query.key}"
      return_data = false

      metric {
        namespace   = "ELSPETH/Operator"
        metric_name = "operator.telemetry.export_failures"
        period      = 300
        stat        = "Maximum"
        dimensions  = metric_query.value
      }
    }
  }

  dynamic "metric_query" {
    for_each = local.cloudwatch_dimension_maps_by_id

    content {
      id          = "drops_${metric_query.key}"
      return_data = false

      metric {
        namespace   = "ELSPETH/Operator"
        metric_name = "operator.telemetry.queue_drops"
        period      = 300
        stat        = "Maximum"
        dimensions  = metric_query.value
      }
    }
  }

  tags = local.tags
}
