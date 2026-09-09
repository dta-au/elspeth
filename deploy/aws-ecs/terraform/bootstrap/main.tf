locals {
  # Mirrors modules/scenario/locals.tf exactly: all scenario namespaces
  # (and the bucket names built from them) are pure functions of run_id,
  # so the boundary below can name only THIS run's resources instead of
  # every a-*/b-*/c-* sibling in the account.
  scenario_namespaces = {
    for scenario_id in ["A", "B", "C"] :
    scenario_id => format(
      "%s-%s",
      lower(scenario_id),
      substr(sha256("${lower(var.run_id)}\u0000${scenario_id}"), 0, 20),
    )
  }
  compact_run_id = replace(var.run_id, "-", "")
  scenario_buckets = [
    for namespace in values(local.scenario_namespaces) :
    "elspeth-${namespace}-${substr(local.compact_run_id, 0, 12)}"
  ]
  gateway_secret_arns = compact([
    var.gateway_bearer_secret_arn,
    var.gateway_oauth_client_id_secret_arn,
    var.gateway_oauth_client_secret_secret_arn,
  ])
  gateway_repository_arns = var.gateway_ecr_repository == "" ? [] : [
    "arn:aws:ecr:${var.aws_region}:${var.aws_account_id}:repository/${var.gateway_ecr_repository}",
  ]
}

resource "aws_s3_bucket" "terraform_state" {
  # No force_destroy: the versioned bucket may hold live state for a
  # scenario the operator has not destroyed (Scenario B shares this
  # backend). Teardown empties it explicitly only after the runbook's
  # census proves every scenario state is resource-free.
  bucket = var.backend_state_bucket
}

resource "aws_s3_bucket_ownership_controls" "terraform_state" {
  bucket = aws_s3_bucket.terraform_state.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_versioning" "terraform_state" {
  bucket = aws_s3_bucket.terraform_state.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "terraform_state" {
  bucket = aws_s3_bucket.terraform_state.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "terraform_state" {
  bucket                  = aws_s3_bucket.terraform_state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_ecr_repository" "acceptance" {
  name                 = var.ecr_repository
  image_tag_mutability = "IMMUTABLE"
  force_delete         = true

  encryption_configuration {
    encryption_type = "AES256"
  }

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "acceptance" {
  # Untagged-only expiry, mirroring the agent repository below. The old
  # tagged rule expired acceptance-* after one day — exactly the tag the
  # runbook publishes — and ECR expiry deletes the image itself, so the
  # deployed digest became unpullable and task replacement failed after
  # day one. Deployed images persist until teardown (force_delete on the
  # repository removes everything).
  repository = aws_ecr_repository.acceptance.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Expire only untagged application images after 30 days"
      selection = {
        tagStatus   = "untagged"
        countType   = "sinceImagePushed"
        countUnit   = "days"
        countNumber = 30
      }
      action = { type = "expire" }
    }]
  })
}

# The agent image has a dedicated repository so short-lived application-image
# policies cannot evict the sidecar required by replacement tasks.
resource "aws_ecr_repository" "cloudwatch_agent" {
  name                 = var.cloudwatch_agent_ecr_repository
  image_tag_mutability = "IMMUTABLE"
  force_delete         = true

  encryption_configuration {
    encryption_type = "AES256"
  }

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "cloudwatch_agent" {
  repository = aws_ecr_repository.cloudwatch_agent.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Expire only untagged CloudWatch agent images after 30 days"
      selection = {
        tagStatus   = "untagged"
        countType   = "sinceImagePushed"
        countUnit   = "days"
        countNumber = 30
      }
      action = { type = "expire" }
    }]
  })
}

data "aws_iam_policy_document" "ecs_permissions_boundary" {
  statement {
    sid = "PullElspethImages"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer",
    ]
    resources = concat(
      [
        "arn:aws:ecr:${var.aws_region}:${var.aws_account_id}:repository/${var.ecr_repository}",
        "arn:aws:ecr:${var.aws_region}:${var.aws_account_id}:repository/${var.cloudwatch_agent_ecr_repository}",
      ],
      local.gateway_repository_arns,
    )
  }

  statement {
    sid       = "AuthenticateToEcr"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid = "UseElspethLogs"
    actions = [
      "logs:CreateLogStream",
      "logs:DescribeLogStreams",
      "logs:PutLogEvents",
    ]
    resources = flatten([
      for namespace in values(local.scenario_namespaces) : [
        "arn:aws:logs:${var.aws_region}:${var.aws_account_id}:log-group:/aws/ecs/${namespace}*",
        "arn:aws:logs:${var.aws_region}:${var.aws_account_id}:log-group:/aws/ecs/${namespace}*:log-stream:*",
        "arn:aws:logs:${var.aws_region}:${var.aws_account_id}:log-group:/aws/ecs/containerinsights/acceptance-${namespace}-cluster/*",
        "arn:aws:logs:${var.aws_region}:${var.aws_account_id}:log-group:/aws/ecs/containerinsights/acceptance-${namespace}-cluster/*:log-stream:*",
      ]
    ])
  }

  statement {
    sid     = "ReadRunSecrets"
    actions = ["secretsmanager:GetSecretValue"]
    resources = concat(
      [
        for namespace in values(local.scenario_namespaces) :
        "arn:aws:secretsmanager:${var.aws_region}:${var.aws_account_id}:secret:${namespace}-database-*"
      ],
      local.gateway_secret_arns,
    )
  }

  statement {
    # Exact bucket names, never elspeth-*: the wildcard also matched every
    # sibling run's bucket AND this run's Terraform state bucket, so a task
    # role widened up to this boundary could read state and cross-run data.
    sid = "UseElspethObjects"
    actions = [
      "s3:DeleteObject",
      "s3:GetObject",
      "s3:GetObjectVersion",
      "s3:PutObject",
    ]
    resources = [for bucket in local.scenario_buckets : "arn:aws:s3:::${bucket}/*"]
  }

  statement {
    # Bucket-level (not object-level) resource: s3:ListBucket targets the bucket, and the
    # runtime task policy needs it so a missing acceptance object reports 404, not 403.
    sid       = "ListElspethBuckets"
    actions   = ["s3:ListBucket"]
    resources = [for bucket in local.scenario_buckets : "arn:aws:s3:::${bucket}"]
  }

  statement {
    sid     = "InvokeBedrockModels"
    actions = ["bedrock:InvokeModel"]
    resources = [
      "arn:aws:bedrock:*:${var.aws_account_id}:inference-profile/*",
      # Cross-region inference profiles invoke destination foundation models.
      # The resource type has no account component, so only its region varies.
      "arn:aws:bedrock:*::foundation-model/*",
    ]
  }

  statement {
    # Guardrail ids are unknowable before the scenario apply, so the run
    # binding is the ACCEPTANCE_RUN_ID tag the scenario stamps on every
    # guardrail rather than a name pattern.
    sid       = "ApplyRunGuardrails"
    actions   = ["bedrock:ApplyGuardrail", "bedrock:GetGuardrail"]
    resources = ["arn:aws:bedrock:${var.aws_region}:${var.aws_account_id}:guardrail/*"]

    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/ACCEPTANCE_RUN_ID"
      values   = [var.run_id]
    }
  }

  statement {
    # Textract's document-analysis actions are not resource-scopable: none of them
    # names an ARN, so IAM offers no narrower resource than "*". The real boundary
    # for the asynchronous pair is the S3 object grant above — StartDocumentAnalysis
    # reads DocumentLocation.S3Object under the caller's own credentials, so a task
    # can only analyse documents it could already read. The synchronous
    # AnalyzeDocument call takes document bytes in the request itself; its inputs
    # come from the deployment's own payload store (the runtime enforces the
    # 5 MiB synchronous bound and byte-signature/format agreement fail-closed).
    sid       = "RunDocumentAnalysis"
    actions   = ["textract:AnalyzeDocument", "textract:StartDocumentAnalysis", "textract:GetDocumentAnalysis"]
    resources = ["*"]
  }

  statement {
    # File-system ids are unknowable before the scenario apply; the run
    # binding is the ACCEPTANCE_RUN_ID tag, mirroring ApplyRunGuardrails.
    sid = "MountRunFileSystems"
    actions = [
      "elasticfilesystem:ClientMount",
      "elasticfilesystem:ClientWrite",
    ]
    resources = ["arn:aws:elasticfilesystem:${var.aws_region}:${var.aws_account_id}:file-system/*"]

    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/ACCEPTANCE_RUN_ID"
      values   = [var.run_id]
    }
  }

  statement {
    sid = "UseEcsExecChannels"
    actions = [
      "ssmmessages:CreateControlChannel",
      "ssmmessages:CreateDataChannel",
      "ssmmessages:OpenControlChannel",
      "ssmmessages:OpenDataChannel",
    ]
    resources = ["*"]
  }

  statement {
    sid = "PublishAndReadRunTelemetry"
    actions = [
      "cloudwatch:GetMetricData",
      "xray:BatchGetTraces",
      "xray:PutTelemetryRecords",
      "xray:PutTraceSegments",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_policy" "ecs_permissions_boundary" {
  provider = aws.iam_lifecycle

  name        = "elspeth-${var.run_id}-ecs-boundary"
  description = "Maximum permissions for disposable ELSPETH ECS task and execution roles."
  policy      = data.aws_iam_policy_document.ecs_permissions_boundary.json
  tags        = { ACCEPTANCE_RUN_ID = var.run_id }
}
