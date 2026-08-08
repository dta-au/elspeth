resource "random_password" "database" {
  length  = 40
  special = false
}

resource "random_password" "database_schema" {
  length  = 40
  special = false
}

resource "random_password" "database_runtime" {
  length  = 40
  special = false
}

resource "random_password" "secret_key" {
  length  = 64
  special = false
}

resource "random_password" "shareable_link" {
  length  = 64
  special = false
}

resource "aws_db_subnet_group" "database" {
  name       = "${local.namespace}-database"
  subnet_ids = aws_subnet.public[*].id

  tags = merge(local.tags, { Name = "${local.namespace}-database" })
}

resource "aws_rds_cluster" "database" {
  cluster_identifier = local.database_identifier
  engine             = "aurora-postgresql"
  engine_version     = var.aurora_engine_version
  engine_mode        = "provisioned"
  database_name      = var.database_name
  master_username    = "elspeth_admin"
  master_password    = random_password.database.result
  port               = 5432

  db_subnet_group_name   = aws_db_subnet_group.database.name
  vpc_security_group_ids = [aws_security_group.database.id]
  storage_encrypted      = true
  deletion_protection    = false
  skip_final_snapshot    = true
  apply_immediately      = true
  copy_tags_to_snapshot  = false

  serverlessv2_scaling_configuration {
    min_capacity = 0.5
    max_capacity = 1.0
  }

  tags = local.tags

  lifecycle {
    precondition {
      condition     = var.aurora_engine_major_version == "16" && var.aurora_engine_version == "16.13"
      error_message = "this package is currently validated only for Aurora PostgreSQL 16.13."
    }
  }
}

resource "aws_rds_cluster_instance" "database" {
  identifier          = local.database_instance
  cluster_identifier  = aws_rds_cluster.database.id
  instance_class      = "db.serverless"
  engine              = aws_rds_cluster.database.engine
  engine_version      = aws_rds_cluster.database.engine_version
  publicly_accessible = false
  ca_cert_identifier  = local.rds_ca_identifier

  tags = local.tags
}

resource "aws_secretsmanager_secret" "runtime" {
  name                    = local.database_runtime_secret_name
  recovery_window_in_days = 0

  tags = local.tags
}

resource "aws_secretsmanager_secret_version" "runtime" {
  secret_id = aws_secretsmanager_secret.runtime.id
  secret_string = jsonencode({
    session_url                = "postgresql+psycopg://${local.database_runtime_role}:${random_password.database_runtime.result}@${aws_rds_cluster.database.endpoint}:5432/${local.session_database}?sslmode=verify-full&sslrootcert=${local.rds_ca_bundle_path}"
    landscape_url              = "postgresql+psycopg://${local.database_runtime_role}:${random_password.database_runtime.result}@${aws_rds_cluster.database.endpoint}:5432/${local.landscape_database}?sslmode=verify-full&sslrootcert=${local.rds_ca_bundle_path}"
    secret_key                 = random_password.secret_key.result
    shareable_link_signing_key = random_password.shareable_link.result
  })
}

resource "aws_secretsmanager_secret" "schema" {
  name                    = local.database_schema_secret_name
  recovery_window_in_days = 0

  tags = local.tags
}

resource "aws_secretsmanager_secret_version" "schema" {
  secret_id = aws_secretsmanager_secret.schema.id
  secret_string = jsonencode({
    session_url   = "postgresql+psycopg://${local.database_schema_role}:${random_password.database_schema.result}@${aws_rds_cluster.database.endpoint}:5432/${local.session_database}?sslmode=verify-full&sslrootcert=${local.rds_ca_bundle_path}"
    landscape_url = "postgresql+psycopg://${local.database_schema_role}:${random_password.database_schema.result}@${aws_rds_cluster.database.endpoint}:5432/${local.landscape_database}?sslmode=verify-full&sslrootcert=${local.rds_ca_bundle_path}"
  })
}

resource "aws_secretsmanager_secret" "bootstrap" {
  name                    = local.database_bootstrap_secret_name
  recovery_window_in_days = 0

  tags = local.tags
}

resource "aws_secretsmanager_secret_version" "bootstrap" {
  secret_id = aws_secretsmanager_secret.bootstrap.id
  secret_string = jsonencode({
    admin_url        = "postgresql://elspeth_admin:${random_password.database.result}@${aws_rds_cluster.database.endpoint}:5432/${var.database_name}?sslmode=verify-full&sslrootcert=${local.rds_ca_bundle_path}" # secret-scan: allow-this-line -- generated value, never literal material
    schema_password  = random_password.database_schema.result
    runtime_password = random_password.database_runtime.result
  })
}

resource "aws_efs_file_system" "data" {
  creation_token = local.efs_creation_token
  encrypted      = true

  lifecycle_policy {
    transition_to_ia = "AFTER_30_DAYS"
  }

  tags = merge(local.tags, { Name = local.efs_creation_token })
}

resource "aws_efs_mount_target" "data" {
  count = 2

  file_system_id  = aws_efs_file_system.data.id
  subnet_id       = aws_subnet.public[count.index].id
  security_groups = [aws_security_group.efs.id]
}

resource "aws_efs_access_point" "data" {
  file_system_id = aws_efs_file_system.data.id

  posix_user {
    uid = 1654
    gid = 1654
  }

  root_directory {
    path = "/elspeth-${local.namespace}"

    creation_info {
      owner_uid   = 1654
      owner_gid   = 1654
      permissions = "0750"
    }
  }

  tags = merge(local.tags, { Name = "${local.namespace}-data" })
}

resource "aws_s3_bucket" "acceptance" {
  bucket        = local.s3_bucket_name
  force_destroy = true

  tags = local.tags
}

resource "aws_s3_bucket_ownership_controls" "acceptance" {
  bucket = aws_s3_bucket.acceptance.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "acceptance" {
  bucket                  = aws_s3_bucket.acceptance.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "acceptance" {
  bucket = aws_s3_bucket.acceptance.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_bedrock_guardrail" "prompt" {
  count = local.bedrock_backend ? 1 : 0

  name                      = "elspeth-${local.compact_run_id}-${local.scenario_id_lower}-prompt"
  description               = "Disposable ELSPETH ${var.scenario_id} prompt acceptance Guardrail"
  blocked_input_messaging   = local.effective_prompt_guardrail.blocked_input_messaging
  blocked_outputs_messaging = local.effective_prompt_guardrail.blocked_outputs_messaging

  content_policy_config {
    dynamic "filters_config" {
      for_each = local.effective_prompt_guardrail.filters

      content {
        type            = filters_config.value.type
        input_strength  = filters_config.value.input_strength
        output_strength = filters_config.value.output_strength
      }
    }
  }

  tags = merge(local.tags, { GuardrailPurpose = "prompt" })
}

resource "aws_bedrock_guardrail_version" "prompt" {
  count = local.bedrock_backend ? 1 : 0

  guardrail_arn = aws_bedrock_guardrail.prompt[count.index].guardrail_arn
  # The content digest in the description forces a NEW immutable version
  # whenever the effective guardrail policy changes. With a constant
  # description the parent updated in place, no new version was cut, and
  # task definitions kept injecting the superseded safety policy.
  description  = "Disposable ELSPETH prompt acceptance version ${substr(sha256(jsonencode(local.effective_prompt_guardrail)), 0, 12)}"
  skip_destroy = false

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_bedrock_guardrail" "content" {
  count = local.bedrock_backend ? 1 : 0

  name                      = "elspeth-${local.compact_run_id}-${local.scenario_id_lower}-content"
  description               = "Disposable ELSPETH ${var.scenario_id} content acceptance Guardrail"
  blocked_input_messaging   = local.effective_content_guardrail.blocked_input_messaging
  blocked_outputs_messaging = local.effective_content_guardrail.blocked_outputs_messaging

  content_policy_config {
    dynamic "filters_config" {
      for_each = local.effective_content_guardrail.filters

      content {
        type            = filters_config.value.type
        input_strength  = filters_config.value.input_strength
        output_strength = filters_config.value.output_strength
      }
    }
  }

  tags = merge(local.tags, { GuardrailPurpose = "content" })
}

resource "aws_bedrock_guardrail_version" "content" {
  count = local.bedrock_backend ? 1 : 0

  guardrail_arn = aws_bedrock_guardrail.content[count.index].guardrail_arn
  # Content digest forces a new version on policy change — see the
  # prompt version resource above for the full rationale.
  description  = "Disposable ELSPETH content acceptance version ${substr(sha256(jsonencode(local.effective_content_guardrail)), 0, 12)}"
  skip_destroy = false

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_cognito_user_pool" "web" {
  count = local.deployment_mode == "upgrade" ? 1 : 0

  name = "${local.namespace}-pool"

  admin_create_user_config {
    allow_admin_create_user_only = true
  }

  username_configuration {
    case_sensitive = false
  }

  password_policy {
    minimum_length                   = 12
    require_lowercase                = true
    require_numbers                  = true
    require_symbols                  = false
    require_uppercase                = true
    temporary_password_validity_days = 1
  }

  tags = local.tags
}

resource "aws_cognito_user_pool_domain" "web" {
  count = local.deployment_mode == "upgrade" ? 1 : 0

  domain       = local.oidc_domain_prefix
  user_pool_id = aws_cognito_user_pool.web[0].id
}

resource "aws_cognito_user_pool_client" "web" {
  count = local.deployment_mode == "upgrade" ? 1 : 0

  name         = "${local.namespace}-public-client"
  user_pool_id = aws_cognito_user_pool.web[0].id

  generate_secret                      = false
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "profile", "email"]
  supported_identity_providers         = ["COGNITO"]
  callback_urls                        = ["https://${aws_lb.web.dns_name}/"]
  logout_urls                          = ["https://${aws_lb.web.dns_name}/"]
  prevent_user_existence_errors        = "ENABLED"
  explicit_auth_flows                  = ["ALLOW_REFRESH_TOKEN_AUTH", "ALLOW_USER_SRP_AUTH"]

  access_token_validity  = 1
  id_token_validity      = 1
  refresh_token_validity = 1

  token_validity_units {
    access_token  = "hours"
    id_token      = "hours"
    refresh_token = "days"
  }
}
