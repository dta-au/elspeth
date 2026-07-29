# Contract tests for the operator-configurable web plugin policy.
#
# Two jobs. First, pin the DEFAULT rendering: these strings are the protected
# plugin-policy assignment the acceptance controller hashes, so an unset
# variable must reproduce exactly what the module produced when these values
# were hardcoded. Second, prove each new knob actually reaches the rendered
# policy. Rejection rules are exercised structurally instead — see the note at
# the foot of this file for why they cannot be expressed here.

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
  purpose                         = "Web plugin policy contract"
  cleanup_deadline                = "2030-01-01T00:00:00Z"
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

# ── The default rendering is the pre-refactor rendering ───────────────────

run "unset_variables_render_the_shipped_policy" {
  command = plan

  assert {
    condition     = output.resolved_inventory.values.ELSPETH_WEB__PLUGIN_ALLOWLIST == "[\"source:aws_s3\",\"source:csv\",\"source:json\",\"source:text\",\"sink:aws_s3\",\"sink:csv\",\"sink:json\",\"sink:text\",\"transform:aws_bedrock_content_safety\",\"transform:aws_bedrock_prompt_shield\",\"transform:aws_textract_document_analysis\",\"transform:field_mapper\",\"transform:llm\",\"transform:passthrough\",\"transform:web_scrape\"]"
    error_message = "the default plugin allowlist must render exactly the shipped 15-plugin set, in order."
  }

  assert {
    condition     = output.resolved_inventory.values.ELSPETH_WEB__PLUGIN_PREFERENCES == "{\"content_safety\":[\"transform:aws_bedrock_content_safety\"],\"prompt_shield\":[\"transform:aws_bedrock_prompt_shield\"]}"
    error_message = "the default plugin preferences must select the two Bedrock control implementations."
  }

  assert {
    condition     = output.resolved_inventory.values.ELSPETH_WEB__PLUGIN_CONTROL_MODES == "{\"content_safety\":\"required\",\"prompt_shield\":\"required\"}"
    error_message = "both controls must default to required; weakening one has to be an explicit operator decision."
  }

  assert {
    condition     = output.resolved_inventory.values.ELSPETH_WEB__LLM_PROFILES == "{\"fast\":{\"model\":\"bedrock/apac.advisor-profile\",\"provider\":\"bedrock\",\"region_name\":\"ap-southeast-1\"},\"standard\":{\"model\":\"bedrock/apac.primary-profile\",\"provider\":\"bedrock\",\"region_name\":\"ap-southeast-1\"}}"
    error_message = "the default profile pair must stay standard=composer model, fast=advisor model, keyless Bedrock."
  }

  assert {
    condition     = output.resolved_inventory.values.ELSPETH_WEB__DEFAULT_LLM_PROFILE == "standard"
    error_message = "the default LLM profile must remain the general-purpose standard tier."
  }
}

# ── Each knob reaches the rendered policy ─────────────────────────────────

run "operator_profiles_replace_the_default_pair" {
  command = plan

  variables {
    llm_profiles = {
      standard = { model = "bedrock/apac.primary-profile" }
      thinking = { model = "bedrock/apac.third-model", region_name = "ap-southeast-2" }
    }
    default_llm_profile = "thinking"
  }

  # A third model used to pass startup validation and then die at invoke with
  # AccessDenied, because the Bedrock grant was derived from the Composer pair
  # alone. It is now derived from these profiles too, so this plans cleanly.
  assert {
    condition     = strcontains(output.resolved_inventory.values.ELSPETH_WEB__LLM_PROFILES, "bedrock/apac.third-model")
    error_message = "an operator-supplied profile must reach the rendered LLM profile policy."
  }

  assert {
    condition     = strcontains(output.resolved_inventory.values.ELSPETH_WEB__LLM_PROFILES, "\"region_name\":\"ap-southeast-2\"")
    error_message = "a profile's explicit region_name must override the ambient deployment region."
  }

  assert {
    condition     = output.resolved_inventory.values.ELSPETH_WEB__DEFAULT_LLM_PROFILE == "thinking"
    error_message = "default_llm_profile must be operator-selectable."
  }
}

run "control_modes_are_operator_selectable" {
  command = plan

  variables {
    plugin_control_modes = {
      prompt_shield  = "recommend"
      content_safety = "required"
    }
  }

  assert {
    condition     = output.resolved_inventory.values.ELSPETH_WEB__PLUGIN_CONTROL_MODES == "{\"content_safety\":\"required\",\"prompt_shield\":\"recommend\"}"
    error_message = "plugin_control_modes must reach the rendered policy so a control can be relaxed to advisory."
  }
}

run "allowlist_is_operator_selectable" {
  command = plan

  variables {
    plugin_allowlist     = ["transform:llm", "transform:field_mapper"]
    plugin_preferences   = {}
    plugin_control_modes = {}
  }

  assert {
    condition     = output.resolved_inventory.values.ELSPETH_WEB__PLUGIN_ALLOWLIST == "[\"transform:llm\",\"transform:field_mapper\"]"
    error_message = "plugin_allowlist must reach the rendered policy verbatim and in order."
  }
}

run "guardrail_policy_is_operator_tunable" {
  command = plan

  variables {
    prompt_guardrail = {
      filters                 = [{ type = "PROMPT_ATTACK", input_strength = "MEDIUM", output_strength = "NONE" }]
      blocked_input_messaging = "Blocked by agency policy."
    }
    content_guardrail = {
      filters = [
        { type = "HATE", input_strength = "NONE", output_strength = "MEDIUM" },
        { type = "VIOLENCE", input_strength = "NONE", output_strength = "HIGH" },
      ]
    }
  }

  assert {
    condition     = length(output.effective_guardrail_policy.content.filters) == 2
    error_message = "the content Guardrail must carry exactly the operator-supplied filter list."
  }

  assert {
    condition     = output.effective_guardrail_policy.content.filters[0].output_strength == "MEDIUM"
    error_message = "an operator-supplied filter strength must reach the Guardrail."
  }

  assert {
    condition     = output.effective_guardrail_policy.prompt.blocked_input_messaging == "Blocked by agency policy."
    error_message = "operator-supplied blocked-input messaging must reach the Guardrail."
  }

  assert {
    condition     = output.effective_guardrail_policy.prompt.blocked_outputs_messaging == "Output blocked by acceptance policy."
    error_message = "an omitted messaging field must fall back to the object attribute default."
  }
}

# ── Rejection cases ───────────────────────────────────────────────────────
#
# The rejection rules live on the MODULE's variables and its
# aws_ecs_task_definition.candidate_web preconditions, and both fire correctly:
# an invalid control mode, a dangling default_llm_profile, a non-Bedrock profile
# model, an out-of-range Guardrail strength, and a "required" control whose
# implementation is absent from the allowlist all abort the plan.
#
# They are not expressed here because `expect_failures` can only name objects
# belonging to the configuration under test. A module-level variable validation
# is reported against `module.scenario`, not against the root `var.*` of the
# same name, and a module-internal resource precondition cannot be named at
# all. Restating every rule on the root variables purely to make it addressable
# would create two copies to keep in sync — the drift is a worse risk than the
# gap.
#
# Those rules are instead pinned structurally in
# tests/unit/deployment/test_aws_ecs_terraform_package.py, which asserts each
# validation and precondition exists with the condition it is supposed to have.
