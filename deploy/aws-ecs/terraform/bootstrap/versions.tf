terraform {
  required_version = ">= 1.14, < 2.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.54.0"
    }
  }
}

provider "aws" {
  region              = var.aws_region
  profile             = var.aws_profile
  allowed_account_ids = [var.aws_account_id]

  default_tags {
    tags = {
      ACCEPTANCE_RUN_ID = var.run_id
      Owner             = var.owner
      Purpose           = var.purpose
      RunId             = var.run_id
      CleanupDeadline   = var.cleanup_deadline
      ManagedBy         = "terraform"
    }
  }
}
