resource "aws_ecs_cluster" "scenario" {
  name = local.cluster_name

  setting {
    name  = "containerInsights"
    value = "enabled"
  }

  tags = local.tags
}

locals {
  ecs_identity_wrapper = <<-SHELL
    metadata_url="$ECS_CONTAINER_METADATA_URI_V4/task"
    family=$(python -c 'import json,sys,urllib.request; print(json.load(urllib.request.urlopen(sys.argv[1], timeout=5))["Family"])' "$metadata_url")
    revision=$(python -c 'import json,sys,urllib.request; print(json.load(urllib.request.urlopen(sys.argv[1], timeout=5))["Revision"])' "$metadata_url")
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
      command     = ["CMD-SHELL", "kill -0 1"]
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

  candidate_web_container = {
    name       = local.web_container_name
    image      = terraform_data.candidate_image_provenance.output
    essential  = true
    entryPoint = ["/bin/sh", "-ceu", local.ecs_identity_wrapper, "--"]
    command    = ["web", "--host", "0.0.0.0", "--port", "8451"]
    dependsOn  = [{ containerName = "cloudwatch-agent", condition = "HEALTHY" }]
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
    environment            = local.runtime_environment
    secrets                = local.schema_owner_secrets
    mountPoints            = local.mount_points
    logConfiguration       = local.doctor_log_configuration
  }

  runtime_doctor_container = {
    name                   = local.doctor_name
    image                  = terraform_data.candidate_image_provenance.output
    essential              = true
    readonlyRootFilesystem = true
    entryPoint             = ["/bin/sh", "-ceu", local.ecs_identity_wrapper, "--"]
    command                = ["doctor", "aws-ecs", "--json"]
    environment            = local.runtime_environment
    secrets                = local.runtime_secrets
    mountPoints            = local.mount_points
    logConfiguration       = local.doctor_log_configuration
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
    image       = terraform_data.rollback_image_provenance.output
    environment = local.rollback_environment
  })
}

resource "aws_ecs_task_definition" "candidate_web" {
  family                   = local.web_family
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 1024
  memory                   = 2048
  task_role_arn            = aws_iam_role.task.arn
  execution_role_arn       = aws_iam_role.execution.arn
  container_definitions    = jsonencode([local.cloudwatch_agent_container, local.candidate_web_container])

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
        for capability, mode in local.effective_plugin_control_modes : [
          for implementation in lookup(local.effective_plugin_preferences, capability, []) :
          contains(local.effective_plugin_allowlist, implementation)
        ]
        if mode == "required"
      ]))
      error_message = "every implementation preferred for a \"required\" control must also appear in plugin_allowlist, or the web service cannot satisfy that control."
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
  cpu                      = 512
  memory                   = 1024
  task_role_arn            = aws_iam_role.task.arn
  execution_role_arn       = aws_iam_role.execution.arn
  container_definitions    = jsonencode([local.runtime_doctor_container])

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
  count = var.scenario_id == "B" ? 1 : 0

  family                   = local.rollback_web_family
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 1024
  memory                   = 2048
  task_role_arn            = aws_iam_role.task.arn
  execution_role_arn       = aws_iam_role.execution.arn
  container_definitions    = jsonencode([local.cloudwatch_agent_container, local.rollback_web_container])

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
  count = var.scenario_id == "B" ? 1 : 0

  family                   = local.rollback_doctor_family
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 512
  memory                   = 1024
  task_role_arn            = aws_iam_role.task.arn
  execution_role_arn       = aws_iam_role.execution.arn
  container_definitions    = jsonencode([local.rollback_doctor_container])

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

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

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
