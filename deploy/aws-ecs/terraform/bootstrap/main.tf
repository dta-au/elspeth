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
