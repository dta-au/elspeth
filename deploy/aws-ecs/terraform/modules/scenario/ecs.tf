resource "aws_ecs_cluster" "scenario" {
  name = local.cluster_name

  depends_on = [aws_cloudwatch_log_group.container_insights]

  setting {
    name  = "containerInsights"
    value = "enabled"
  }

  tags = local.tags
}

locals {
  ecs_identity_wrapper = <<-SHELL
    identity=$(python -m elspeth.web._aws_ecs_acceptance.ecs_metadata)
    family=$${identity% *}
    revision=$${identity#* }
    export ELSPETH_WEB__OPERATOR_TELEMETRY_TASK_DEFINITION_FAMILY="$family"
    export ELSPETH_WEB__OPERATOR_TELEMETRY_TASK_DEFINITION_REVISION="$revision"
    case "$1" in
      web|doctor) set -- elspeth "$@" ;;
    esac
    exec "$@"
  SHELL

  efs_volume = {
    name = "data"
    efsVolumeConfiguration = {
      fileSystemId          = aws_efs_file_system.data.id
      rootDirectory         = "/"
      transitEncryption     = "ENABLED"
      transitEncryptionPort = 2999
      authorizationConfig = {
        accessPointId = aws_efs_access_point.data.id
        iam           = "ENABLED"
      }
    }
  }

  mount_points = [{
    sourceVolume  = "data"
    containerPath = local.data_dir
    readOnly      = false
  }]

  web_log_configuration = {
    logDriver = "awslogs"
    options = {
      awslogs-group         = aws_cloudwatch_log_group.web.name
      awslogs-region        = var.aws_region
      awslogs-stream-prefix = "web"
    }
  }

  doctor_log_configuration = {
    logDriver = "awslogs"
    options = {
      awslogs-group         = aws_cloudwatch_log_group.doctor.name
      awslogs-region        = var.aws_region
      awslogs-stream-prefix = "doctor"
    }
  }

  cloudwatch_agent_container = {
    name              = "cloudwatch-agent"
    image             = terraform_data.cloudwatch_agent_image_provenance.output
    essential         = false
    memoryReservation = 192
    entryPoint        = ["/bin/sh", "-ceu"]
    command           = [local.cw_agent_command]
    environment = [
      { name = "ELSPETH_CW_AGENT_CONFIG_JSON_B64", value = base64encode(local.cw_agent_json) },
      { name = "ELSPETH_CW_AGENT_CONFIG_JSON_SHA256", value = local.cw_agent_config_json_sha256 },
      { name = "ELSPETH_CW_AGENT_OTEL_YAML_B64", value = base64encode(local.cw_agent_otel) },
      { name = "ELSPETH_CW_AGENT_OTEL_YAML_SHA256", value = local.cw_agent_otel_yaml_sha256 },
    ]
    healthCheck = {
      command     = ["CMD", "python", "-c", "import socket; socket.create_connection(('127.0.0.1', 4317), timeout=3).close()"]
      interval    = 10
      timeout     = 5
      retries     = 6
      startPeriod = 30
    }
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.web.name
        awslogs-region        = var.aws_region
        awslogs-stream-prefix = "cloudwatch-agent"
      }
    }
  }

  # Loopback-only gateway sidecar (custom_gateway backend). Essential: its
  # exit makes the whole task unhealthy and ECS replaces the complete unit.
  # The Dockerfile ENTRYPOINT binds 0.0.0.0 for standalone use; this task
  # definition overrides it to 127.0.0.1 so the awsvpc task ENI never serves
  # the gateway port, and no portMappings entry exists for it. Only the
  # inbound bearer and OAuth client credentials reach this container — no
  # ELSPETH application or database secret, no EFS mount.
  gateway_container = {
    name                   = local.gateway_container_name
    image                  = one(terraform_data.gateway_image_provenance[*].output)
    essential              = true
    readonlyRootFilesystem = true
    user                   = "65532:65532"
    cpu                    = 512
    memory                 = 1024
    entryPoint = [
      "/venv/bin/python", "-m", "uvicorn", "elspeth_llm_gateway.core.app:build",
      "--host", "127.0.0.1", "--port", tostring(local.gateway_port),
      "--factory", "--timeout-graceful-shutdown", "30",
    ]
    environment = [
      { name = "ELSPETH_LLM_GATEWAY_ADAPTER", value = var.gateway_adapter },
      { name = "ELSPETH_LLM_GATEWAY_UPSTREAM_ORIGIN", value = var.gateway_upstream_origin },
      { name = "ELSPETH_LLM_GATEWAY_OAUTH_TOKEN_URL", value = var.gateway_oauth_token_url },
      { name = "ELSPETH_LLM_GATEWAY_MODEL_MAPPINGS", value = var.gateway_model_mappings_json },
    ]
    secrets = [
      { name = "ELSPETH_LLM_GATEWAY_INBOUND_BEARER", valueFrom = var.gateway_bearer_secret_arn },
      { name = "ELSPETH_LLM_GATEWAY_OAUTH_CLIENT_ID", valueFrom = var.gateway_oauth_client_id_secret_arn },
      { name = "ELSPETH_LLM_GATEWAY_OAUTH_CLIENT_SECRET", valueFrom = var.gateway_oauth_client_secret_secret_arn },
    ]
    healthCheck = {
      command     = ["CMD", "/venv/bin/python", "-c", "import http.client,sys; c=http.client.HTTPConnection('127.0.0.1',${local.gateway_port},timeout=5); c.request('GET','/readyz'); sys.exit(0 if c.getresponse().status == 200 else 1)"]
      interval    = 10
      timeout     = 6
      retries     = 6
      startPeriod = 30
    }
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.web.name
        awslogs-region        = var.aws_region
        awslogs-stream-prefix = "llm-gateway"
      }
    }
  }

  # Web waits for the gateway to validate configuration and adapter loading
  # before it starts, so a misconfigured sidecar fails the deployment rather
  # than the first user turn.
  service_web_depends_on = concat(
    [{ containerName = "cloudwatch-agent", condition = "HEALTHY" }],
    local.bedrock_backend ? [] : [{ containerName = local.gateway_container_name, condition = "HEALTHY" }],
  )

  # Explicit task sizing: the sidecar's reservations are ADDED, never carved
  # out of the Web allocation (integration design §Service task topology).
  service_task_cpu    = local.bedrock_backend ? 1024 : 1536
  service_task_memory = local.bedrock_backend ? 2048 : 3072

  candidate_web_container = {
    name       = local.web_container_name
    image      = terraform_data.candidate_image_provenance.output
    essential  = true
    entryPoint = ["/bin/sh", "-ceu", local.ecs_identity_wrapper, "--"]
    command    = ["web", "--host", "0.0.0.0", "--port", "8451"]
    dependsOn  = local.service_web_depends_on
    portMappings = [{
      containerPort = 8451
      hostPort      = 8451
      protocol      = "tcp"
    }]
    environment = concat(local.runtime_environment, [
      { name = "OTEL_EXPORTER_OTLP_ENDPOINT", value = "http://127.0.0.1:4317" },
      { name = "OTEL_EXPORTER_OTLP_PROTOCOL", value = "grpc" },
    ])
    secrets     = local.runtime_secrets
    mountPoints = local.mount_points
    healthCheck = {
      command     = ["CMD", "python", "-c", "import http.client,sys; c=http.client.HTTPConnection('127.0.0.1',8451,timeout=5); c.request('GET','/api/health'); r=c.getresponse(); sys.exit(0 if r.status == 200 else 1)"]
      interval    = 30
      timeout     = 6
      retries     = 3
      startPeriod = 150
    }
    logConfiguration = local.web_log_configuration
  }

  schema_init_doctor_container = {
    name                   = local.doctor_name
    image                  = terraform_data.candidate_image_provenance.output
    essential              = true
    readonlyRootFilesystem = true
    entryPoint             = ["/bin/sh", "-ceu", local.ecs_identity_wrapper, "--"]
    command                = ["doctor", "aws-ecs", "--init-schema", "--json"]
    environment = [
      for item in local.runtime_environment :
      item.name == "ELSPETH_WEB__OPERATOR_TELEMETRY_TASK_DEFINITION_FAMILY" ?
      merge(item, { value = local.schema_init_doctor_family }) : item
    ]
    secrets          = local.schema_owner_secrets
    mountPoints      = local.mount_points
    logConfiguration = local.doctor_log_configuration
  }

  runtime_doctor_container = {
    name                   = local.doctor_name
    image                  = terraform_data.candidate_image_provenance.output
    essential              = true
    readonlyRootFilesystem = true
    entryPoint             = ["/bin/sh", "-ceu", local.ecs_identity_wrapper, "--"]
    command                = ["doctor", "aws-ecs", "--json"]
    environment = [
      for item in local.runtime_environment :
      item.name == "ELSPETH_WEB__OPERATOR_TELEMETRY_TASK_DEFINITION_FAMILY" ?
      merge(item, { value = local.runtime_doctor_family }) : item
    ]
    secrets          = local.runtime_secrets
    mountPoints      = local.mount_points
    logConfiguration = local.doctor_log_configuration
  }

  payload_container = {
    name                   = local.web_container_name
    image                  = terraform_data.candidate_image_provenance.output
    essential              = true
    readonlyRootFilesystem = true
    user                   = "1654:1654"
    entryPoint             = ["python", "-m", "elspeth.web.aws_ecs_acceptance"]
    command                = ["provision-storage"]
    environment            = local.runtime_environment
    secrets                = local.runtime_secrets
    mountPoints            = local.mount_points
    logConfiguration       = local.web_log_configuration
  }

  local_auth_container = {
    name                   = local.web_container_name
    image                  = terraform_data.candidate_image_provenance.output
    essential              = true
    readonlyRootFilesystem = true
    user                   = "1654:1654"
    entryPoint             = ["python", "-m", "elspeth.web.aws_ecs_acceptance"]
    command                = ["verify-local-auth"]
    environment            = local.runtime_environment
    secrets                = local.runtime_secrets
    mountPoints            = local.mount_points
    logConfiguration       = local.web_log_configuration
  }

  rollback_environment = [
    for item in local.runtime_environment :
    item.name == "ELSPETH_WEB__OPERATOR_TELEMETRY_RELEASE" ? merge(item, { value = var.rollback_baseline_sha }) :
    item.name == "ELSPETH_WEB__OPERATOR_TELEMETRY_TASK_DEFINITION_FAMILY" ? merge(item, { value = local.rollback_web_family }) : item
  ]

  rollback_web_container = merge(local.candidate_web_container, {
    image = terraform_data.rollback_image_provenance.output
    environment = concat(local.rollback_environment, [
      { name = "OTEL_EXPORTER_OTLP_ENDPOINT", value = "http://127.0.0.1:4317" },
      { name = "OTEL_EXPORTER_OTLP_PROTOCOL", value = "grpc" },
    ])
  })

  rollback_doctor_container = merge(local.runtime_doctor_container, {
    image = terraform_data.rollback_image_provenance.output
    environment = [
      for item in local.rollback_environment :
      item.name == "ELSPETH_WEB__OPERATOR_TELEMETRY_TASK_DEFINITION_FAMILY" ?
      merge(item, { value = local.rollback_doctor_family }) : item
    ]
  })
}

resource "aws_ecs_task_definition" "candidate_web" {
  family                   = local.web_family
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = local.service_task_cpu
  memory                   = local.service_task_memory
  task_role_arn            = aws_iam_role.task.arn
  execution_role_arn       = aws_iam_role.execution.arn
  container_definitions = jsonencode(concat(
    [local.cloudwatch_agent_container, local.candidate_web_container],
    local.bedrock_backend ? [] : [local.gateway_container],
  ))

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = local.cpu_architecture
  }

  dynamic "volume" {
    for_each = [local.efs_volume]
    content {
      name = volume.value.name
      efs_volume_configuration {
        file_system_id          = volume.value.efsVolumeConfiguration.fileSystemId
        root_directory          = volume.value.efsVolumeConfiguration.rootDirectory
        transit_encryption      = volume.value.efsVolumeConfiguration.transitEncryption
        transit_encryption_port = volume.value.efsVolumeConfiguration.transitEncryptionPort
        authorization_config {
          access_point_id = volume.value.efsVolumeConfiguration.authorizationConfig.accessPointId
          iam             = volume.value.efsVolumeConfiguration.authorizationConfig.iam
        }
      }
    }
  }

  tags = local.tags

  # The web service compiles the plugin policy at startup and fails closed on an
  # inconsistent one, which means the operator learns about it only after the
  # service is rolled. These preconditions move both failures to `terraform
  # plan`, before an image is registered against a policy that cannot boot.
  lifecycle {
    precondition {
      condition = alltrue([
        for capability, mode in local.effective_plugin_control_modes :
        length(lookup(local.effective_plugin_preferences, capability, [])) > 0
        if mode == "required"
      ])
      error_message = "a control set to \"required\" in plugin_control_modes must name at least one implementation in plugin_preferences."
    }

    precondition {
      condition = alltrue(flatten([
        for implementations in values(local.effective_plugin_preferences) : [
          for implementation in implementations :
          contains(local.effective_authorized_plugin_ids, implementation)
        ]
      ]))
      error_message = "every preferred implementation must be authorized by the always-authorized web core or plugin_allowlist."
    }
  }
}

resource "aws_ecs_task_definition" "schema_init_doctor" {
  family                   = local.schema_init_doctor_family
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 512
  memory                   = 1024
  task_role_arn            = aws_iam_role.task.arn
  execution_role_arn       = aws_iam_role.execution.arn
  container_definitions    = jsonencode([local.schema_init_doctor_container])

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = local.cpu_architecture
  }

  volume {
    name = local.efs_volume.name
    efs_volume_configuration {
      file_system_id          = local.efs_volume.efsVolumeConfiguration.fileSystemId
      root_directory          = local.efs_volume.efsVolumeConfiguration.rootDirectory
      transit_encryption      = local.efs_volume.efsVolumeConfiguration.transitEncryption
      transit_encryption_port = local.efs_volume.efsVolumeConfiguration.transitEncryptionPort
      authorization_config {
        access_point_id = local.efs_volume.efsVolumeConfiguration.authorizationConfig.accessPointId
        iam             = local.efs_volume.efsVolumeConfiguration.authorizationConfig.iam
      }
    }
  }

  tags = local.tags
}

resource "aws_ecs_task_definition" "runtime_doctor" {
  family                   = local.runtime_doctor_family
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = local.bedrock_backend ? 512 : 1024
  memory                   = local.bedrock_backend ? 1024 : 2048
  task_role_arn            = aws_iam_role.task.arn
  execution_role_arn       = aws_iam_role.execution.arn
  # The runtime doctor exercises LLM health, so under custom_gateway it
  # launches the IDENTICAL pinned sidecar and waits for its readiness — it
  # must never probe a host-side or substitute gateway. Schema-init and
  # storage-only tasks make no LLM call and deliberately omit the sidecar.
  container_definitions = (
    local.bedrock_backend
    ? jsonencode([local.runtime_doctor_container])
    : jsonencode([
      merge(local.runtime_doctor_container, {
        dependsOn = [{ containerName = local.gateway_container_name, condition = "HEALTHY" }]
      }),
      local.gateway_container,
    ])
  )

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = local.cpu_architecture
  }

  volume {
    name = local.efs_volume.name
    efs_volume_configuration {
      file_system_id          = local.efs_volume.efsVolumeConfiguration.fileSystemId
      root_directory          = local.efs_volume.efsVolumeConfiguration.rootDirectory
      transit_encryption      = local.efs_volume.efsVolumeConfiguration.transitEncryption
      transit_encryption_port = local.efs_volume.efsVolumeConfiguration.transitEncryptionPort
      authorization_config {
        access_point_id = local.efs_volume.efsVolumeConfiguration.authorizationConfig.accessPointId
        iam             = local.efs_volume.efsVolumeConfiguration.authorizationConfig.iam
      }
    }
  }

  tags = local.tags
}

resource "aws_ecs_task_definition" "payload" {
  family                   = local.payload_family
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 512
  memory                   = 1024
  task_role_arn            = aws_iam_role.task.arn
  execution_role_arn       = aws_iam_role.execution.arn
  container_definitions    = jsonencode([local.payload_container])

  # Under custom_gateway this task's runtime_secrets carry the gateway
  # bearer selector, so registration must sit behind the gateway identity
  # gate like every other selector-bearing task definition — the pre-secret
  # admission boundary covers ALL task definitions, not only those that run
  # the gateway container (elspeth-ef60d2ff3c review GAP-1). Empty and
  # therefore a no-op under bedrock.
  depends_on = [terraform_data.gateway_image_provenance]

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = local.cpu_architecture
  }

  volume {
    name = local.efs_volume.name
    efs_volume_configuration {
      file_system_id          = local.efs_volume.efsVolumeConfiguration.fileSystemId
      root_directory          = local.efs_volume.efsVolumeConfiguration.rootDirectory
      transit_encryption      = local.efs_volume.efsVolumeConfiguration.transitEncryption
      transit_encryption_port = local.efs_volume.efsVolumeConfiguration.transitEncryptionPort
      authorization_config {
        access_point_id = local.efs_volume.efsVolumeConfiguration.authorizationConfig.accessPointId
        iam             = local.efs_volume.efsVolumeConfiguration.authorizationConfig.iam
      }
    }
  }

  tags = local.tags
}

resource "aws_ecs_task_definition" "local_auth" {
  family                   = local.local_auth_family
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 512
  memory                   = 1024
  task_role_arn            = aws_iam_role.task.arn
  execution_role_arn       = aws_iam_role.execution.arn
  container_definitions    = jsonencode([local.local_auth_container])

  # Same pre-secret boundary as the payload task definition above: the
  # gateway bearer selector in runtime_secrets requires the gateway
  # identity gate to have passed first (elspeth-ef60d2ff3c review GAP-1).
  depends_on = [terraform_data.gateway_image_provenance]

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = local.cpu_architecture
  }

  volume {
    name = local.efs_volume.name
    efs_volume_configuration {
      file_system_id          = local.efs_volume.efsVolumeConfiguration.fileSystemId
      root_directory          = local.efs_volume.efsVolumeConfiguration.rootDirectory
      transit_encryption      = local.efs_volume.efsVolumeConfiguration.transitEncryption
      transit_encryption_port = local.efs_volume.efsVolumeConfiguration.transitEncryptionPort
      authorization_config {
        access_point_id = local.efs_volume.efsVolumeConfiguration.authorizationConfig.accessPointId
        iam             = local.efs_volume.efsVolumeConfiguration.authorizationConfig.iam
      }
    }
  }

  tags = local.tags
}

resource "aws_ecs_task_definition" "rollback_web" {
  count = local.deployment_mode == "upgrade" ? 1 : 0

  family                   = local.rollback_web_family
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 1024
  memory                   = 2048
  task_role_arn            = aws_iam_role.task.arn
  execution_role_arn       = aws_iam_role.execution.arn
  container_definitions    = jsonencode([local.cloudwatch_agent_container, local.rollback_web_container])

  # The rollback containers merge() the web/doctor bases and inherit
  # runtime_secrets, so under custom_gateway they would carry the gateway
  # bearer selector. Today upgrade + custom_gateway cannot co-occur (the
  # maintained-triple validation in variables.tf pins scenario C to a
  # first deployment), but the pre-secret admission boundary must not rest
  # on that coupling alone (elspeth-ef60d2ff3c re-verification residual).
  # Empty and therefore a no-op under bedrock.
  depends_on = [terraform_data.gateway_image_provenance]

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = local.cpu_architecture
  }

  volume {
    name = local.efs_volume.name
    efs_volume_configuration {
      file_system_id          = local.efs_volume.efsVolumeConfiguration.fileSystemId
      root_directory          = local.efs_volume.efsVolumeConfiguration.rootDirectory
      transit_encryption      = local.efs_volume.efsVolumeConfiguration.transitEncryption
      transit_encryption_port = local.efs_volume.efsVolumeConfiguration.transitEncryptionPort
      authorization_config {
        access_point_id = local.efs_volume.efsVolumeConfiguration.authorizationConfig.accessPointId
        iam             = local.efs_volume.efsVolumeConfiguration.authorizationConfig.iam
      }
    }
  }

  tags = local.tags
}

resource "aws_ecs_task_definition" "rollback_doctor" {
  count = local.deployment_mode == "upgrade" ? 1 : 0

  family                   = local.rollback_doctor_family
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 512
  memory                   = 1024
  task_role_arn            = aws_iam_role.task.arn
  execution_role_arn       = aws_iam_role.execution.arn
  container_definitions    = jsonencode([local.rollback_doctor_container])

  # Same latent-selector boundary as rollback_web above
  # (elspeth-ef60d2ff3c re-verification residual).
  depends_on = [terraform_data.gateway_image_provenance]

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = local.cpu_architecture
  }

  volume {
    name = local.efs_volume.name
    efs_volume_configuration {
      file_system_id          = local.efs_volume.efsVolumeConfiguration.fileSystemId
      root_directory          = local.efs_volume.efsVolumeConfiguration.rootDirectory
      transit_encryption      = local.efs_volume.efsVolumeConfiguration.transitEncryption
      transit_encryption_port = local.efs_volume.efsVolumeConfiguration.transitEncryptionPort
      authorization_config {
        access_point_id = local.efs_volume.efsVolumeConfiguration.authorizationConfig.accessPointId
        iam             = local.efs_volume.efsVolumeConfiguration.authorizationConfig.iam
      }
    }
  }

  tags = local.tags
}

resource "aws_ecs_service" "web" {
  name                               = local.service_name
  cluster                            = aws_ecs_cluster.scenario.id
  task_definition                    = aws_ecs_task_definition.candidate_web.arn
  desired_count                      = 0
  launch_type                        = "FARGATE"
  platform_version                   = "1.4.0"
  enable_execute_command             = true
  health_check_grace_period_seconds  = 150
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100
  wait_for_steady_state              = false

  # rollback stays OFF: the README's upgrade contract makes the previous
  # image ineligible after the settings/secret transition (it crash-loops
  # on the rotated canonical URLs), so an automatic rollback restores a
  # known-bad deployment while reading as recovery. The breaker still
  # halts a failing deployment; the runbook's posture is fix forward.
  deployment_circuit_breaker {
    enable   = true
    rollback = false
  }

  # The acceptance service is enabled (scaled up) outside Terraform, so a
  # destroy can meet running tasks; force_delete lets teardown proceed
  # instead of wedging on a service it cannot scale down itself.
  force_delete = true

  network_configuration {
    subnets          = aws_subnet.public[*].id
    security_groups  = [aws_security_group.task.id]
    assign_public_ip = true
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.web.arn
    container_name   = local.web_container_name
    container_port   = 8451
  }

  lifecycle {
    ignore_changes = [desired_count, task_definition]
  }

  depends_on = [
    aws_lb_listener_rule.traffic,
    aws_efs_mount_target.data,
    aws_rds_cluster_instance.database,
  ]

  tags = local.tags
}
