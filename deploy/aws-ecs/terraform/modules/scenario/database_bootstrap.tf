locals {
  database_bootstrap_script = <<-PY
    import os
    from urllib.parse import urlsplit, urlunsplit

    import psycopg
    from psycopg import sql

    admin_url = os.environ["ELSPETH_DB_ADMIN_URL"]
    schema_role = os.environ["ELSPETH_DB_SCHEMA_ROLE"]
    runtime_role = os.environ["ELSPETH_DB_RUNTIME_ROLE"]
    schema_password = os.environ["ELSPETH_DB_SCHEMA_PASSWORD"]
    runtime_password = os.environ["ELSPETH_DB_RUNTIME_PASSWORD"]
    databases = [
        os.environ["ELSPETH_DB_SESSION_DATABASE"],
        os.environ["ELSPETH_DB_LANDSCAPE_DATABASE"],
    ]

    def database_url(database: str) -> str:
        parsed = urlsplit(admin_url)
        return urlunsplit((parsed.scheme, parsed.netloc, f"/{database}", parsed.query, ""))

    with psycopg.connect(admin_url, autocommit=True) as connection:
        admin_role = connection.execute("SELECT current_user").fetchone()[0]
        for role, password in ((schema_role, schema_password), (runtime_role, runtime_password)):
            exists = connection.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
            if exists:
                connection.execute(
                    sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}")
                    .format(sql.Identifier(role), sql.Literal(password))
                )
            else:
                connection.execute(
                    sql.SQL("CREATE ROLE {} WITH LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION")
                    .format(sql.Identifier(role), sql.Literal(password))
                )
        connection.execute(
            sql.SQL("GRANT {} TO {} WITH ADMIN OPTION").format(
                sql.Identifier(schema_role),
                sql.Identifier(admin_role),
            )
        )
        for database in databases:
            exists = connection.execute("SELECT 1 FROM pg_database WHERE datname = %s", (database,)).fetchone()
            if not exists:
                connection.execute(
                    sql.SQL("CREATE DATABASE {} OWNER {}").format(
                        sql.Identifier(database),
                        sql.Identifier(schema_role),
                    )
                )
            connection.execute(
                sql.SQL("ALTER DATABASE {} OWNER TO {}").format(
                    sql.Identifier(database),
                    sql.Identifier(schema_role),
                )
            )
            connection.execute(
                sql.SQL("REVOKE CREATE ON DATABASE {} FROM {}").format(
                    sql.Identifier(database),
                    sql.Identifier(runtime_role),
                )
            )
            connection.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                    sql.Identifier(database),
                    sql.Identifier(runtime_role),
                )
            )

    for database in databases:
        with psycopg.connect(database_url(database), autocommit=True) as connection:
            connection.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
            connection.execute(
                sql.SQL("REVOKE CREATE ON SCHEMA public FROM {}").format(sql.Identifier(runtime_role))
            )
            connection.execute(
                sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(runtime_role))
            )
            connection.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(schema_role)))
            connection.execute(
                sql.SQL("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {}").format(
                    sql.Identifier(runtime_role)
                )
            )
            connection.execute(
                sql.SQL("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO {}").format(
                    sql.Identifier(runtime_role)
                )
            )
            connection.execute(
                sql.SQL("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT EXECUTE ON FUNCTIONS TO {}").format(
                    sql.Identifier(runtime_role)
                )
            )
            connection.execute("RESET ROLE")
  PY

  database_bootstrap_container = {
    name       = "database-bootstrap"
    image      = var.candidate_image
    essential  = true
    entryPoint = ["python", "-c"]
    command    = [local.database_bootstrap_script]
    environment = [
      { name = "ELSPETH_DB_SCHEMA_ROLE", value = local.database_schema_role },
      { name = "ELSPETH_DB_RUNTIME_ROLE", value = local.database_runtime_role },
      { name = "ELSPETH_DB_SESSION_DATABASE", value = local.session_database },
      { name = "ELSPETH_DB_LANDSCAPE_DATABASE", value = local.landscape_database },
    ]
    secrets          = local.database_bootstrap_secrets
    logConfiguration = local.doctor_log_configuration
  }

  database_bootstrap_network_configuration = jsonencode({
    awsvpcConfiguration = {
      subnets        = aws_subnet.public[*].id
      securityGroups = [aws_security_group.task.id]
      assignPublicIp = "ENABLED"
    }
  })
}

resource "aws_ecs_task_definition" "database_bootstrap" {
  family                   = local.database_bootstrap_family
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 512
  memory                   = 1024
  task_role_arn            = aws_iam_role.task.arn
  execution_role_arn       = aws_iam_role.execution.arn
  container_definitions    = jsonencode([local.database_bootstrap_container])

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = local.cpu_architecture
  }

  tags = local.tags
}

resource "terraform_data" "database_bootstrap" {
  triggers_replace = [
    aws_rds_cluster_instance.database.id,
    aws_ecs_task_definition.database_bootstrap.arn,
  ]

  depends_on = [aws_secretsmanager_secret_version.bootstrap]

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-ceu"]
    command     = <<-SHELL
      work=$(mktemp -d -p /tmp elspeth-database-bootstrap.XXXXXX)
      chmod 700 "$work"
      trap 'rm -rf -- "$work"' EXIT
      if ! task_arn=$(aws ecs run-task \
        --region "$AWS_REGION" \
        --cluster "$ECS_CLUSTER" \
        --task-definition "$TASK_DEFINITION" \
        --launch-type FARGATE \
        --network-configuration "$NETWORK_CONFIGURATION" \
        --count 1 \
        --query 'tasks[0].taskArn' \
        --output text 2>"$work/run.err"); then
        printf '%s\n' database_bootstrap_run_failed >&2
        exit 1
      fi
      test -n "$task_arn" && test "$task_arn" != None || {
        printf '%s\n' database_bootstrap_task_missing >&2
        exit 1
      }
      if ! aws ecs wait tasks-stopped \
        --region "$AWS_REGION" \
        --cluster "$ECS_CLUSTER" \
        --tasks "$task_arn" >"$work/wait.out" 2>"$work/wait.err"; then
        printf '%s\n' database_bootstrap_wait_failed >&2
        exit 1
      fi
      if ! exit_code=$(aws ecs describe-tasks \
        --region "$AWS_REGION" \
        --cluster "$ECS_CLUSTER" \
        --tasks "$task_arn" \
        --query 'tasks[0].containers[?name==`database-bootstrap`].exitCode | [0]' \
        --output text 2>"$work/describe.err"); then
        printf '%s\n' database_bootstrap_describe_failed >&2
        exit 1
      fi
      test "$exit_code" = 0 || {
        printf '%s\n' database_bootstrap_task_failed >&2
        exit 1
      }
    SHELL

    environment = {
      AWS_PAGER             = ""
      AWS_REGION            = var.aws_region
      ECS_CLUSTER           = aws_ecs_cluster.scenario.name
      TASK_DEFINITION       = aws_ecs_task_definition.database_bootstrap.arn
      NETWORK_CONFIGURATION = local.database_bootstrap_network_configuration
    }
  }
}
