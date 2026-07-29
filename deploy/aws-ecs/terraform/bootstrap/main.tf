resource "aws_s3_bucket" "terraform_state" {
  bucket        = var.backend_state_bucket
  force_destroy = true
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
  repository = aws_ecr_repository.acceptance.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Expire temporary acceptance images"
      selection = {
        tagStatus     = "tagged"
        tagPrefixList = ["acceptance-"]
        countType     = "sinceImagePushed"
        countUnit     = "days"
        countNumber   = 1
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
    resources = ["arn:aws:ecr:${var.aws_region}:${var.aws_account_id}:repository/elspeth-*"]
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
    resources = [
      "arn:aws:logs:${var.aws_region}:${var.aws_account_id}:log-group:/aws/ecs/*",
      "arn:aws:logs:${var.aws_region}:${var.aws_account_id}:log-group:/aws/ecs/*:log-stream:*",
    ]
  }

  statement {
    sid     = "ReadRunSecrets"
    actions = ["secretsmanager:GetSecretValue"]
    resources = [
      "arn:aws:secretsmanager:${var.aws_region}:${var.aws_account_id}:secret:a-*-database-*",
      "arn:aws:secretsmanager:${var.aws_region}:${var.aws_account_id}:secret:b-*-database-*",
    ]
  }

  statement {
    sid = "UseElspethObjects"
    actions = [
      "s3:DeleteObject",
      "s3:GetObject",
      "s3:PutObject",
    ]
    resources = ["arn:aws:s3:::elspeth-*/*"]
  }

  statement {
    # Bucket-level (not object-level) resource: s3:ListBucket targets the bucket, and the
    # runtime task policy needs it so a missing acceptance object reports 404, not 403.
    sid       = "ListElspethBuckets"
    actions   = ["s3:ListBucket"]
    resources = ["arn:aws:s3:::elspeth-*"]
  }

  statement {
    sid     = "InvokeBedrockModels"
    actions = ["bedrock:InvokeModel"]
    resources = [
      "arn:aws:bedrock:${var.aws_region}:${var.aws_account_id}:inference-profile/*",
      # Cross-region inference profiles invoke destination foundation models.
      # The resource type has no account component, so only its region varies.
      "arn:aws:bedrock:*::foundation-model/*",
    ]
  }

  statement {
    sid       = "ApplyRunGuardrails"
    actions   = ["bedrock:ApplyGuardrail", "bedrock:GetGuardrail"]
    resources = ["arn:aws:bedrock:${var.aws_region}:${var.aws_account_id}:guardrail/*"]
  }

  statement {
    # Textract's asynchronous document-analysis pair is not resource-scopable: neither
    # StartDocumentAnalysis nor GetDocumentAnalysis names an ARN, so IAM offers no
    # narrower resource than "*". The real boundary is the S3 object grant above —
    # StartDocumentAnalysis reads DocumentLocation.S3Object under the caller's own
    # credentials, so a task can only analyse documents it could already read.
    sid       = "RunDocumentAnalysis"
    actions   = ["textract:StartDocumentAnalysis", "textract:GetDocumentAnalysis"]
    resources = ["*"]
  }

  statement {
    sid = "MountRunFileSystems"
    actions = [
      "elasticfilesystem:ClientMount",
      "elasticfilesystem:ClientWrite",
    ]
    resources = ["arn:aws:elasticfilesystem:${var.aws_region}:${var.aws_account_id}:file-system/*"]
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
