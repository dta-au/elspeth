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
  provider             = aws.iam_lifecycle
  assume_role_policy   = data.aws_iam_policy_document.ecs_tasks_assume.json
  permissions_boundary = var.iam_permissions_boundary_arn

  tags = local.tags
}

resource "aws_iam_role" "execution" {
  name                 = "${local.namespace}-execution-role"
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
    sid     = "InvokeConfiguredBedrockModels"
    actions = ["bedrock:InvokeModel"]
    resources = concat(
      tolist(var.bedrock_inference_profile_arns),
      tolist(var.bedrock_foundation_model_arns),
      local.bedrock_cross_region_foundation_model_arns,
    )
  }

  statement {
    sid       = "ApplyAcceptanceGuardrails"
    actions   = ["bedrock:ApplyGuardrail", "bedrock:GetGuardrail"]
    resources = [aws_bedrock_guardrail.prompt.guardrail_arn, aws_bedrock_guardrail.content.guardrail_arn]
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
  cloudwatch_dimensions = [
    "service.name", local.telemetry_service_name,
    "deployment.environment", "production",
    "service.version", var.candidate_sha,
    "cloud.provider", "aws",
    "aws.ecs.cluster.name", local.cluster_name,
    "aws.ecs.service.name", local.service_name,
    "aws.ecs.task.family", local.web_family,
    "aws.ecs.task.revision", tostring(aws_ecs_task_definition.candidate_web.revision),
  ]

  cloudwatch_dimension_map = {
    "service.name"           = local.telemetry_service_name
    "deployment.environment" = "production"
    "service.version"        = var.candidate_sha
    "cloud.provider"         = "aws"
    "aws.ecs.cluster.name"   = local.cluster_name
    "aws.ecs.service.name"   = local.service_name
    "aws.ecs.task.family"    = local.web_family
    "aws.ecs.task.revision"  = tostring(aws_ecs_task_definition.candidate_web.revision)
  }

  metric_widgets = [
    {
      title   = "Run failures and duration"
      metrics = ["run.failure", "run.duration"]
      stat    = "Sum"
    },
    {
      title   = "External-call failures and latency"
      metrics = ["external_call.failure", "external_call.latency"]
      stat    = "Sum"
    },
    {
      title   = "LLM token and cost totals"
      metrics = ["llm.prompt_tokens", "llm.completion_tokens", "llm.cost"]
      stat    = "Sum"
    },
    {
      title = "Composer and runtime failures"
      metrics = [
        "composer.runtime_preflight_total",
        "composer.authoring_validation_total",
        "composer.audit.fetch_failure_total",
      ]
      stat = "Sum"
    },
    {
      title = "Operator delivery health"
      metrics = [
        "operator.telemetry.export_failures",
        "operator.telemetry.queue_drops",
        "operator.telemetry.last_success_age_seconds",
        "operator.telemetry.collector_unavailable",
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
          metrics = [
            for metric_name in widget.metrics : concat(
              ["ELSPETH/Operator", metric_name],
              local.cloudwatch_dimensions,
            )
          ]
        }
      }],
    )
  })
}

resource "aws_cloudwatch_metric_alarm" "operator_direct" {
  for_each = local.direct_alarm_specs

  alarm_name          = "${local.namespace}-${each.key}"
  alarm_description   = each.value.owner_action
  namespace           = "ELSPETH/Operator"
  metric_name         = each.value.metric_name
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = each.value.evaluation_periods
  period              = each.value.period
  statistic           = each.value.statistic
  extended_statistic  = each.value.extended_statistic
  threshold           = each.value.threshold
  treat_missing_data  = each.value.treat_missing_data
  alarm_actions       = concat([aws_sns_topic.operator_alarms.arn], var.alarm_actions)
  dimensions          = local.cloudwatch_dimension_map

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

  metric_query {
    id          = "combined"
    expression  = "failures + drops"
    label       = "Export failures plus queue drops"
    return_data = true
  }

  metric_query {
    id          = "failures"
    return_data = false

    metric {
      namespace   = "ELSPETH/Operator"
      metric_name = "operator.telemetry.export_failures"
      period      = 300
      stat        = "Maximum"
      dimensions  = local.cloudwatch_dimension_map
    }
  }

  metric_query {
    id          = "drops"
    return_data = false

    metric {
      namespace   = "ELSPETH/Operator"
      metric_name = "operator.telemetry.queue_drops"
      period      = 300
      stat        = "Maximum"
      dimensions  = local.cloudwatch_dimension_map
    }
  }

  tags = local.tags
}

locals {
  cloudwatch_agent_image = var.cloudwatch_agent_image
}
