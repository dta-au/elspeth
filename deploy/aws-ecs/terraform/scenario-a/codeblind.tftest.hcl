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

run "standalone_codeblind_plan" {
  command = plan

  variables {
    run_id                    = "12345678-1234-4123-8123-123456789abc"
    scenario_id               = "A"
    candidate_sha             = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    candidate_image           = format("%s.dkr.ecr.ap-southeast-1.amazonaws.com/elspeth-web@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", join("", ["123456", "789012"]))
    aws_account_id            = join("", ["123456", "789012"])
    aws_region                = "ap-southeast-1"
    aws_profile               = "mock-test"
    iam_lifecycle_aws_profile = "mock-iam-lifecycle"
    iam_permissions_boundary_arn = format(
      "arn:aws:iam::%s:policy/elspeth-%s-ecs-boundary",
      join("", ["123456", "789012"]),
      "12345678-1234-4123-8123-123456789abc",
    )
    owner                  = "terraform-test"
    purpose                = "Standalone Scenario A mocked plan"
    cleanup_deadline       = "2030-01-01T00:00:00Z"
    cloudwatch_agent_image = format("%s.dkr.ecr.ap-southeast-1.amazonaws.com/elspeth-cloudwatch-agent@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", join("", ["123456", "789012"]))
    composer_model         = "bedrock/apac.primary-profile"
    composer_advisor_model = "bedrock/apac.advisor-profile"

    bedrock_inference_profile_arns = [
      format("arn:aws:bedrock:ap-southeast-1:%s:inference-profile/apac.primary-profile", join("", ["123456", "789012"])),
      format("arn:aws:bedrock:ap-southeast-1:%s:inference-profile/apac.advisor-profile", join("", ["123456", "789012"])),
    ]
    bedrock_foundation_model_arns = [
      "arn:aws:bedrock:ap-southeast-2::foundation-model/primary-model",
      "arn:aws:bedrock:ap-southeast-3::foundation-model/advisor-model",
    ]
  }

  assert {
    condition     = output.resolved_inventory.scenario_id == "A"
    error_message = "The standalone mocked plan must resolve Scenario A."
  }
}
