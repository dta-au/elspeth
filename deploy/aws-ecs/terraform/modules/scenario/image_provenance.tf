locals {
  candidate_image_config_contract = "elspeth.aws-ecs.runtime.v1"
}

# ADMISSION TRUST MODEL (elspeth-9f7d336e1c residual): these checks prove
# that the image the operator named is the image ECR serves (every
# reference is a digest, and the pull is by that digest) and that its
# self-declared org.opencontainers.image.revision label matches the SHA
# the operator claims, and that its declared settings contract matches this
# package. Producer identity remains an operator handoff responsibility:
# verify the CI cosign signature before copying the image into ECR; see README
# "Minimum image revision".
resource "terraform_data" "candidate_image_provenance" {
  input = var.candidate_image

  triggers_replace = [
    var.candidate_image,
    var.candidate_sha,
    var.candidate_ecr_repository,
    var.target_platform,
    local.candidate_image_config_contract,
  ]

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-ceu"]
    command     = <<-SHELL
      registry="$AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com"
      work=$(mktemp -d -p /tmp elspeth-candidate-provenance.XXXXXX)
      chmod 700 "$work"
      mkdir -m 700 "$work/docker-config"
      export DOCKER_CONFIG="$work/docker-config"
      trap 'docker logout "$registry" >/dev/null 2>&1 || true; rm -rf -- "$work"' EXIT

      aws ecr get-login-password \
        --profile "$AWS_PROFILE" \
        --region "$AWS_REGION" \
        >"$work/ecr-password"
      docker login \
        --username AWS \
        --password-stdin "$registry" \
        <"$work/ecr-password" \
        >"$work/docker-login.out"
      docker pull --platform "$TARGET_PLATFORM" "$CANDIDATE_IMAGE" >"$work/docker-pull.out"
      # A --platform pull is a selection preference, not an assertion: a
      # single-manifest image of the wrong architecture still pulls. Assert
      # the admitted bytes' architecture explicitly (elspeth-ef60d2ff3c
      # review GAP-2).
      architecture=$(docker image inspect --format '{{.Architecture}}' "$CANDIDATE_IMAGE")
      test "$architecture" = "$EXPECTED_ARCHITECTURE" || {
        printf '%s\n' candidate_image_architecture_mismatch >&2
        exit 1
      }
      # The design's admission bar: an unavailable scan blocks before secret
      # injection. COMPLETE = basic scanning finished; ACTIVE = enhanced
      # continuous scanning. Severity policy stays an operator decision —
      # this gate only refuses to admit an UNSCANNED image.
      scan_status=$(aws ecr describe-image-scan-findings \
        --profile "$AWS_PROFILE" \
        --region "$AWS_REGION" \
        --repository-name "$ECR_REPOSITORY" \
        --image-id imageDigest="$${CANDIDATE_IMAGE#*@}" \
        --query 'imageScanStatus.status' \
        --output text 2>"$work/scan-status.err") || {
        printf '%s\n' candidate_image_scan_unavailable >&2
        cat "$work/scan-status.err" >&2 || true
        exit 1
      }
      case "$scan_status" in
        COMPLETE|ACTIVE) : ;;
        *)
          printf '%s observed=%s\n' candidate_image_scan_unavailable "$scan_status" >&2
          exit 1
          ;;
      esac
      config_contract=$(docker image inspect \
        --format '{{ index .Config.Labels "io.elspeth.aws-ecs-config-contract" }}' \
        "$CANDIDATE_IMAGE")
      test "$config_contract" = "$EXPECTED_CONFIG_CONTRACT" || {
        printf '%s\n' candidate_image_config_contract_mismatch >&2
        exit 1
      }
      revision=$(docker image inspect \
        --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' \
        "$CANDIDATE_IMAGE")
      test "$revision" = "$CANDIDATE_SHA" || {
        printf '%s\n' candidate_image_revision_mismatch >&2
        exit 1
      }
    SHELL

    environment = {
      AWS_ACCOUNT_ID           = var.aws_account_id
      AWS_PROFILE              = var.aws_profile
      AWS_REGION               = var.aws_region
      CANDIDATE_IMAGE          = var.candidate_image
      CANDIDATE_SHA            = var.candidate_sha
      ECR_REPOSITORY           = var.candidate_ecr_repository
      EXPECTED_ARCHITECTURE    = split("/", var.target_platform)[1]
      EXPECTED_CONFIG_CONTRACT = local.candidate_image_config_contract
      TARGET_PLATFORM          = var.target_platform
    }
  }
}

resource "terraform_data" "rollback_image_provenance" {
  input = var.rollback_baseline_image

  triggers_replace = [
    var.rollback_baseline_image,
    var.rollback_baseline_sha,
    var.candidate_ecr_repository,
    var.target_platform,
    local.candidate_image_config_contract,
  ]

  depends_on = [terraform_data.candidate_image_provenance]

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-ceu"]
    command     = <<-SHELL
      registry="$AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com"
      work=$(mktemp -d -p /tmp elspeth-rollback-provenance.XXXXXX)
      chmod 700 "$work"
      mkdir -m 700 "$work/docker-config"
      export DOCKER_CONFIG="$work/docker-config"
      trap 'docker logout "$registry" >/dev/null 2>&1 || true; rm -rf -- "$work"' EXIT

      aws ecr get-login-password \
        --profile "$AWS_PROFILE" \
        --region "$AWS_REGION" \
        >"$work/ecr-password"
      docker login \
        --username AWS \
        --password-stdin "$registry" \
        <"$work/ecr-password" \
        >"$work/docker-login.out"
      docker pull --platform "$TARGET_PLATFORM" "$ROLLBACK_IMAGE" >"$work/docker-pull.out"
      config_contract=$(docker image inspect \
        --format '{{ index .Config.Labels "io.elspeth.aws-ecs-config-contract" }}' \
        "$ROLLBACK_IMAGE")
      test "$config_contract" = "$EXPECTED_CONFIG_CONTRACT" || {
        printf '%s\n' rollback_image_config_contract_mismatch >&2
        exit 1
      }
      revision=$(docker image inspect \
        --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' \
        "$ROLLBACK_IMAGE")
      test "$revision" = "$ROLLBACK_SHA" || {
        printf '%s\n' rollback_image_revision_mismatch >&2
        exit 1
      }
    SHELL

    environment = {
      AWS_ACCOUNT_ID           = var.aws_account_id
      AWS_PROFILE              = var.aws_profile
      AWS_REGION               = var.aws_region
      EXPECTED_CONFIG_CONTRACT = local.candidate_image_config_contract
      ROLLBACK_IMAGE           = var.rollback_baseline_image
      ROLLBACK_SHA             = var.rollback_baseline_sha
      TARGET_PLATFORM          = var.target_platform
    }
  }
}

resource "terraform_data" "cloudwatch_agent_image_provenance" {
  input = var.cloudwatch_agent_image

  triggers_replace = [
    var.cloudwatch_agent_image,
    var.candidate_sha,
    var.cloudwatch_agent_ecr_repository,
    var.target_platform,
  ]

  depends_on = [terraform_data.rollback_image_provenance]

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-ceu"]
    command     = <<-SHELL
      registry="$AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com"
      work=$(mktemp -d -p /tmp elspeth-cloudwatch-agent-provenance.XXXXXX)
      chmod 700 "$work"
      mkdir -m 700 "$work/docker-config"
      export DOCKER_CONFIG="$work/docker-config"
      trap 'docker logout "$registry" >/dev/null 2>&1 || true; rm -rf -- "$work"' EXIT

      aws ecr get-login-password \
        --profile "$AWS_PROFILE" \
        --region "$AWS_REGION" \
        >"$work/ecr-password"
      docker login \
        --username AWS \
        --password-stdin "$registry" \
        <"$work/ecr-password" \
        >"$work/docker-login.out"
      docker pull --platform "$TARGET_PLATFORM" "$CLOUDWATCH_AGENT_IMAGE" >"$work/docker-pull.out"
      revision=$(docker image inspect \
        --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' \
        "$CLOUDWATCH_AGENT_IMAGE")
      test "$revision" = "$CANDIDATE_SHA" || {
        printf '%s\n' cloudwatch_agent_image_revision_mismatch >&2
        exit 1
      }
    SHELL

    environment = {
      AWS_ACCOUNT_ID         = var.aws_account_id
      AWS_PROFILE            = var.aws_profile
      AWS_REGION             = var.aws_region
      CANDIDATE_SHA          = var.candidate_sha
      CLOUDWATCH_AGENT_IMAGE = var.cloudwatch_agent_image
      TARGET_PLATFORM        = var.target_platform
    }
  }
}

# Gateway sidecar admission (custom_gateway backend only). The complete
# identity gate runs OFFLINE, before any credential-bearing task definition
# is registered (elspeth-ef60d2ff3c): digest-pinned pull from the approved
# registry, revision-label match against the operator-claimed SHA, and exact
# matches on the image's identity labels — base runtime identity, supported
# gateway contract major, and the adapter's name, version, API major, and
# package fingerprint. The service task definition consumes this resource's
# output, and every other selector-bearing task definition (payload,
# local_auth) depends on it, so any mismatch blocks before the gateway or
# OAuth secret selectors exist anywhere. This gate is the ONLY place
# ADAPTER identity (name, version, api-major, fingerprint) is compared
# against an operator expectation: the runtime does verify contract_major
# against its own configuration when it reads /readyz, but it only
# REPORTS the adapter quadruple and enforces the SDK's own api-major
# self-consistency — it never compares the adapter's identity against an
# expected value (see gateway providers' gateway.py docstring) — treat
# runtime adapter-identity reporting as evidence, not enforcement. The same candidate-gate
# caveat applies: labels prove what the image declares, not who built it —
# the supported pairing remains an image and expectations supplied
# together by the operator.
resource "terraform_data" "gateway_image_provenance" {
  count = local.bedrock_backend ? 0 : 1

  input = var.gateway_image

  triggers_replace = [
    var.gateway_image,
    var.gateway_sha,
    var.gateway_ecr_repository,
    var.gateway_adapter,
    var.gateway_adapter_version,
    var.gateway_adapter_api_major,
    var.gateway_adapter_fingerprint,
    var.target_platform,
  ]

  depends_on = [terraform_data.cloudwatch_agent_image_provenance]

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-ceu"]
    command     = <<-SHELL
      registry="$AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com"
      work=$(mktemp -d -p /tmp elspeth-gateway-provenance.XXXXXX)
      chmod 700 "$work"
      mkdir -m 700 "$work/docker-config"
      export DOCKER_CONFIG="$work/docker-config"
      trap 'docker logout "$registry" >/dev/null 2>&1 || true; rm -rf -- "$work"' EXIT

      aws ecr get-login-password \
        --profile "$AWS_PROFILE" \
        --region "$AWS_REGION" \
        >"$work/ecr-password"
      docker login \
        --username AWS \
        --password-stdin "$registry" \
        <"$work/ecr-password" \
        >"$work/docker-login.out"
      docker pull --platform "$TARGET_PLATFORM" "$GATEWAY_IMAGE" >"$work/docker-pull.out"
      architecture=$(docker image inspect --format '{{.Architecture}}' "$GATEWAY_IMAGE")
      test "$architecture" = "$EXPECTED_ARCHITECTURE" || {
        printf '%s\n' gateway_image_architecture_mismatch >&2
        exit 1
      }
      scan_status=$(aws ecr describe-image-scan-findings \
        --profile "$AWS_PROFILE" \
        --region "$AWS_REGION" \
        --repository-name "$ECR_REPOSITORY" \
        --image-id imageDigest="$${GATEWAY_IMAGE#*@}" \
        --query 'imageScanStatus.status' \
        --output text 2>"$work/scan-status.err") || {
        printf '%s\n' gateway_image_scan_unavailable >&2
        cat "$work/scan-status.err" >&2 || true
        exit 1
      }
      case "$scan_status" in
        COMPLETE|ACTIVE) : ;;
        *)
          printf '%s observed=%s\n' gateway_image_scan_unavailable "$scan_status" >&2
          exit 1
          ;;
      esac
      revision=$(docker image inspect \
        --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' \
        "$GATEWAY_IMAGE")
      test "$revision" = "$GATEWAY_SHA" || {
        printf '%s\n' gateway_image_revision_mismatch >&2
        exit 1
      }
      for check in \
        "io.elspeth.llm-gateway.runtime-identity|$EXPECTED_RUNTIME_IDENTITY|gateway_image_runtime_identity_mismatch" \
        "io.elspeth.llm-gateway.contract-major|$EXPECTED_CONTRACT_MAJOR|gateway_image_contract_major_mismatch" \
        "io.elspeth.llm-gateway.adapter-name|$EXPECTED_ADAPTER_NAME|gateway_image_adapter_name_mismatch" \
        "io.elspeth.llm-gateway.adapter-version|$EXPECTED_ADAPTER_VERSION|gateway_image_adapter_version_mismatch" \
        "io.elspeth.llm-gateway.adapter-api-major|$EXPECTED_ADAPTER_API_MAJOR|gateway_image_adapter_api_major_mismatch" \
        "io.elspeth.llm-gateway.adapter-fingerprint|$EXPECTED_ADAPTER_FINGERPRINT|gateway_image_adapter_fingerprint_mismatch"
      do
        label=$${check%%|*}
        rest=$${check#*|}
        expected=$${rest%%|*}
        error_token=$${rest#*|}
        actual=$(docker image inspect \
          --format "{{ index .Config.Labels \"$label\" }}" \
          "$GATEWAY_IMAGE")
        test "$actual" = "$expected" || {
          printf '%s\n' "$error_token" >&2
          exit 1
        }
      done
    SHELL

    environment = {
      AWS_ACCOUNT_ID               = var.aws_account_id
      AWS_PROFILE                  = var.aws_profile
      AWS_REGION                   = var.aws_region
      ECR_REPOSITORY               = var.gateway_ecr_repository
      EXPECTED_ARCHITECTURE        = split("/", var.target_platform)[1]
      GATEWAY_IMAGE                = var.gateway_image
      GATEWAY_SHA                  = var.gateway_sha
      EXPECTED_RUNTIME_IDENTITY    = "elspeth-llm-gateway"
      EXPECTED_CONTRACT_MAJOR      = "1"
      EXPECTED_ADAPTER_NAME        = var.gateway_adapter
      EXPECTED_ADAPTER_VERSION     = var.gateway_adapter_version
      EXPECTED_ADAPTER_API_MAJOR   = tostring(var.gateway_adapter_api_major)
      EXPECTED_ADAPTER_FINGERPRINT = var.gateway_adapter_fingerprint
      TARGET_PLATFORM              = var.target_platform
    }
  }
}
