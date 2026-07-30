mock_provider "aws" {
  mock_data "aws_availability_zones" {
    defaults = {
      names = ["ap-southeast-1a", "ap-southeast-1b"]
    }
  }

  mock_data "aws_iam_policy_document" {
    defaults = {
      json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
    }
  }
}
mock_provider "aws" {
  alias = "iam_lifecycle"
}
mock_provider "random" {}
mock_provider "tls" {}

variables {
  run_id                    = "12345678-1234-4123-8123-123456789abc"
  scenario_id               = "A"
  candidate_sha             = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  candidate_image           = format("%s.dkr.ecr.ap-southeast-1.amazonaws.com/elspeth-web@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", join("", ["123456", "789012"]))
  candidate_ecr_repository  = "elspeth-web"
  aws_account_id            = join("", ["123456", "789012"])
  aws_region                = "ap-southeast-1"
  aws_profile               = "mock-test"
  iam_lifecycle_aws_profile = "mock-iam-lifecycle"
  iam_permissions_boundary_arn = format(
    "arn:aws:iam::%s:policy/elspeth-%s-ecs-boundary",
    join("", ["123456", "789012"]),
    "12345678-1234-4123-8123-123456789abc",
  )
  owner                           = "terraform-test"
  purpose                         = "Standalone Scenario A mocked plan"
  cleanup_deadline                = "2030-01-01T00:00:00Z"
  alb_https_ingress_cidrs         = ["203.0.113.42/32"]
  cloudwatch_agent_image          = format("%s.dkr.ecr.ap-southeast-1.amazonaws.com/elspeth-cloudwatch-agent@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", join("", ["123456", "789012"]))
  cloudwatch_agent_ecr_repository = "elspeth-cloudwatch-agent"
  composer_model                  = "bedrock/apac.primary-profile"
  composer_advisor_model          = "bedrock/apac.advisor-profile"
  bedrock_inference_profile_arns = [
    format("arn:aws:bedrock:ap-southeast-1:%s:inference-profile/apac.primary-profile", join("", ["123456", "789012"])),
    format("arn:aws:bedrock:ap-southeast-1:%s:inference-profile/apac.advisor-profile", join("", ["123456", "789012"])),
  ]
  bedrock_foundation_model_arns = [
    "arn:aws:bedrock:ap-southeast-2::foundation-model/primary-model",
    "arn:aws:bedrock:ap-southeast-3::foundation-model/advisor-model",
  ]
}

run "standalone_codeblind_plan" {
  command = plan

  assert {
    condition     = output.resolved_inventory.scenario_id == "A"
    error_message = "The standalone mocked plan must resolve Scenario A."
  }
}

run "reject_foreign_registry_candidate" {
  command = plan

  variables {
    candidate_image = "ghcr.io/example/elspeth@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  }

  expect_failures = [var.candidate_image]
}

run "reject_foreign_account_candidate" {
  command = plan

  variables {
    candidate_image = format("%s.dkr.ecr.ap-southeast-1.amazonaws.com/elspeth-web@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", join("", ["999999", "999999"]))
  }

  expect_failures = [var.candidate_image]
}

run "reject_foreign_repository_candidate" {
  command = plan

  variables {
    candidate_image = format("%s.dkr.ecr.ap-southeast-1.amazonaws.com/attacker@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", join("", ["123456", "789012"]))
  }

  expect_failures = [var.candidate_image]
}

run "reject_dot_wildcard_repository_candidate" {
  command = plan

  variables {
    candidate_ecr_repository = "elspeth-web.prod"
    candidate_image          = format("%s.dkr.ecr.ap-southeast-1.amazonaws.com/elspeth-webXprod@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", join("", ["123456", "789012"]))
  }

  expect_failures = [var.candidate_image]
}

run "reject_world_open_https_ingress" {
  command = plan

  variables {
    alb_https_ingress_cidrs = ["0.0.0.0/0"]
  }

  expect_failures = [var.alb_https_ingress_cidrs]
}

run "reject_noncanonical_world_open_https_ingress" {
  command = plan

  variables {
    alb_https_ingress_cidrs = ["198.51.100.7/0"]
  }

  expect_failures = [var.alb_https_ingress_cidrs]
}

run "reject_unsafe_database_identifier" {
  command = plan

  variables {
    database_name = "elspeth; DROP DATABASE postgres"
  }

  expect_failures = [var.database_name]
}

run "reject_shared_runtime_database_names" {
  command = plan

  variables {
    session_database_name   = "shared_runtime"
    landscape_database_name = "shared_runtime"
  }

  expect_failures = [var.landscape_database_name]
}
