# ADMISSION TRUST MODEL (elspeth-9f7d336e1c residual): these checks prove
# that the image the operator named is the image ECR serves (every
# reference is a digest, and the pull is by that digest) and that its
# self-declared org.opencontainers.image.revision label matches the SHA
# the operator claims. They do NOT prove producer identity — the label is
# writable by any builder, and CI's cosign signature is not verified on
# the GHCR-to-ECR handoff — and they do NOT prove settings-contract
# compatibility: a truthfully-labelled older image passes admission and
# then fails every task at settings load. The supported pairing remains a
# package and image cut from the same commit by the operator who supplies
# both values; see README "Minimum image revision".
resource "terraform_data" "candidate_image_provenance" {
  input = var.candidate_image

  triggers_replace = [
    var.candidate_image,
    var.candidate_sha,
    var.candidate_ecr_repository,
    var.target_platform,
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
      revision=$(docker image inspect \
        --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' \
        "$CANDIDATE_IMAGE")
      test "$revision" = "$CANDIDATE_SHA" || {
        printf '%s\n' candidate_image_revision_mismatch >&2
        exit 1
      }
    SHELL

    environment = {
      AWS_ACCOUNT_ID  = var.aws_account_id
      AWS_PROFILE     = var.aws_profile
      AWS_REGION      = var.aws_region
      CANDIDATE_IMAGE = var.candidate_image
      CANDIDATE_SHA   = var.candidate_sha
      TARGET_PLATFORM = var.target_platform
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
      revision=$(docker image inspect \
        --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' \
        "$ROLLBACK_IMAGE")
      test "$revision" = "$ROLLBACK_SHA" || {
        printf '%s\n' rollback_image_revision_mismatch >&2
        exit 1
      }
    SHELL

    environment = {
      AWS_ACCOUNT_ID  = var.aws_account_id
      AWS_PROFILE     = var.aws_profile
      AWS_REGION      = var.aws_region
      ROLLBACK_IMAGE  = var.rollback_baseline_image
      ROLLBACK_SHA    = var.rollback_baseline_sha
      TARGET_PLATFORM = var.target_platform
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

# Gateway sidecar admission (custom_gateway backend only). Same trust model
# as the candidate image: digest-pinned pull from the approved registry plus
# a revision-label match against the operator-claimed SHA, before any
# credential-bearing task definition is registered. Deeper identity claims
# (adapter package fingerprint, contract major) are verified by the gateway
# itself at readiness — /readyz fails if the installed adapter does not match
# ELSPETH_LLM_GATEWAY_ADAPTER — and Web will not start until the gateway is
# HEALTHY, so a wrong-adapter image never receives live traffic.
resource "terraform_data" "gateway_image_provenance" {
  count = local.bedrock_backend ? 0 : 1

  input = var.gateway_image

  triggers_replace = [
    var.gateway_image,
    var.gateway_sha,
    var.gateway_ecr_repository,
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
      revision=$(docker image inspect \
        --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' \
        "$GATEWAY_IMAGE")
      test "$revision" = "$GATEWAY_SHA" || {
        printf '%s\n' gateway_image_revision_mismatch >&2
        exit 1
      }
    SHELL

    environment = {
      AWS_ACCOUNT_ID  = var.aws_account_id
      AWS_PROFILE     = var.aws_profile
      AWS_REGION      = var.aws_region
      GATEWAY_IMAGE   = var.gateway_image
      GATEWAY_SHA     = var.gateway_sha
      TARGET_PLATFORM = var.target_platform
    }
  }
}
