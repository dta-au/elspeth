# Contract tests for Scenario C: first + custom_gateway.
#
# Three jobs. First, prove the acceptance criterion directly: this root plans
# with NO Bedrock model or ARN input anywhere in the variables below. Second,
# pin the gateway-mode policy rendering (gateway-provider profiles, empty
# guardrail surface, no Bedrock live-test model). Third, prove the rejection
# side: a bedrock/ composer model does not plan.
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
  scenario_id               = "C"
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
  purpose                         = "Scenario C gateway contract"
  cleanup_deadline                = "2030-01-01T00:00:00Z"
  alb_https_ingress_cidrs         = ["203.0.113.42/32"]
  cloudwatch_agent_image          = format("%s.dkr.ecr.ap-southeast-1.amazonaws.com/elspeth-cloudwatch-agent@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", join("", ["123456", "789012"]))
  cloudwatch_agent_ecr_repository = "elspeth-cloudwatch-agent"
  composer_model                  = "agency-primary"
  composer_advisor_model          = "agency-advisor"

  # Fail-closed control posture: stated explicitly because the module refuses
  # its Bedrock-implemented defaults under custom_gateway.
  plugin_allowlist = [
    "source:aws_s3",
    "source:csv",
    "source:json",
    "source:llm",
    "source:text",
    "sink:aws_s3",
    "sink:csv",
    "sink:document",
    "sink:json",
    "sink:text",
    "transform:aws_textract_document_analysis",
    "transform:field_mapper",
    "transform:line_explode",
    "transform:llm",
    "transform:passthrough",
    "transform:report_assemble",
    "transform:web_scrape",
  ]
  plugin_preferences = {}
  plugin_control_modes = {
    prompt_shield  = "recommend"
    content_safety = "recommend"
  }

  gateway_image                          = format("%s.dkr.ecr.ap-southeast-1.amazonaws.com/elspeth-llm-gateway@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc", join("", ["123456", "789012"]))
  gateway_sha                            = "cccccccccccccccccccccccccccccccccccccccc"
  gateway_ecr_repository                 = "elspeth-llm-gateway"
  gateway_bearer_secret_arn              = format("arn:aws:secretsmanager:ap-southeast-1:%s:secret:elspeth-gateway-bearer-AbCdEf", join("", ["123456", "789012"]))
  gateway_oauth_client_id_secret_arn     = format("arn:aws:secretsmanager:ap-southeast-1:%s:secret:elspeth-gateway-oauth-id-AbCdEf", join("", ["123456", "789012"]))
  gateway_oauth_client_secret_secret_arn = format("arn:aws:secretsmanager:ap-southeast-1:%s:secret:elspeth-gateway-oauth-secret-AbCdEf", join("", ["123456", "789012"]))
  gateway_adapter                        = "reference_v1"
  gateway_adapter_version                = "0.1.0"
  gateway_adapter_api_major              = 1
  gateway_adapter_fingerprint            = "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
  gateway_upstream_origin                = "https://agency.example.invalid"
  gateway_oauth_token_url                = "https://auth.example.invalid/oauth2/token"
  gateway_model_mappings_json = jsonencode({
    agency-primary = { upstream_model = "primary-upstream" }
    agency-advisor = { upstream_model = "advisor-upstream" }
  })
}

# ── The acceptance criterion: plans with no Bedrock input ──────────────────

run "plans_as_first_custom_gateway_without_bedrock_inputs" {
  command = plan

  assert {
    condition     = output.resolved_inventory.values.DEPLOYMENT_MODE == "first"
    error_message = "Scenario C must plan as a first (cold-install) deployment."
  }

  assert {
    condition     = output.resolved_inventory.gateway_image != "" && output.resolved_inventory.gateway_sha != "" && output.resolved_inventory.gateway_adapter == "reference_v1"
    error_message = "the resolved inventory must identify the gateway sidecar image, revision, and adapter."
  }

  assert {
    condition     = output.resolved_inventory.gateway_adapter_version == "0.1.0" && output.resolved_inventory.gateway_adapter_api_major == 1 && output.resolved_inventory.gateway_adapter_fingerprint == "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
    error_message = "the resolved inventory must bind the verified adapter version, API major, and package fingerprint (inventory schema v9)."
  }

  assert {
    condition     = output.resolved_inventory.values.ELSPETH_BEDROCK_LIVE_TEST_MODEL == ""
    error_message = "no Bedrock live-test model may be named under custom_gateway."
  }

  assert {
    condition     = output.resolved_inventory.values.ELSPETH_WEB__BEDROCK_GUARDRAIL_PROFILES == "[]" && output.resolved_inventory.values.ELSPETH_WEB__BEDROCK_GUARDRAIL_DEFAULT_PROFILES == "{}"
    error_message = "the Bedrock guardrail surface must render empty under custom_gateway."
  }

  assert {
    condition = alltrue([
      for id in jsondecode(output.resolved_inventory.values.ELSPETH_WEB__PLUGIN_ALLOWLIST) :
      !startswith(id, "transform:aws_bedrock_")
    ])
    error_message = "no Bedrock-implemented plugin may be authorized under custom_gateway."
  }
}

# ── Gateway-provider profile rendering ─────────────────────────────────────

run "profiles_lower_to_gateway_bindings" {
  command = plan

  assert {
    condition = alltrue([
      for alias, profile in jsondecode(output.resolved_inventory.values.ELSPETH_WEB__LLM_PROFILES) : (
        profile.provider == "gateway" &&
        profile.endpoint == "http://127.0.0.1:8787/v1" &&
        profile.credential_scope == "server" &&
        profile.credential_ref == "GATEWAY_BEARER" &&
        profile.contract_major == 1
      )
    ])
    error_message = "every rendered profile must be a gateway binding: loopback endpoint, server-scoped GATEWAY_BEARER ref, contract major 1."
  }

  assert {
    condition     = join(",", keys(jsondecode(output.resolved_inventory.values.ELSPETH_WEB__LLM_PROFILES))) == "fast,standard"
    error_message = "the default profile pair (standard/fast) must be derived from the composer models."
  }
}

# ── Rejection: a Bedrock model input does not plan ─────────────────────────

run "bedrock_composer_model_is_rejected" {
  command = plan

  variables {
    composer_model = "bedrock/apac.primary-profile"
  }

  expect_failures = [var.composer_model]
}

run "malformed_gateway_sha_is_rejected" {
  command = plan

  variables {
    gateway_sha = "not-a-sha"
  }

  expect_failures = [var.gateway_sha]
}
