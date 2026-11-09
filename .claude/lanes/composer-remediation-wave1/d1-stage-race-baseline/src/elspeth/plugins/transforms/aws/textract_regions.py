"""Closed Amazon Textract region vocabulary shared by runtime and Web policy."""

from __future__ import annotations

import re

TEXTRACT_REGIONS: frozenset[str] = frozenset(
    {
        "ap-northeast-2",
        "ap-south-1",
        "ap-southeast-1",
        "ap-southeast-2",
        "ca-central-1",
        "eu-central-1",
        "eu-south-2",
        "eu-west-1",
        "eu-west-2",
        "eu-west-3",
        "us-east-1",
        "us-east-2",
        "us-west-1",
        "us-west-2",
    }
)
TEXTRACT_INVARIANT_PROBE_REGION = "ap-southeast-2"

_AWS_REGION_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def is_well_formed_aws_region(value: str) -> bool:
    """Return whether ``value`` is a bounded lower-case AWS region identifier."""
    return 1 <= len(value) <= 64 and _AWS_REGION_PATTERN.fullmatch(value) is not None


def is_supported_textract_region(value: str | None) -> bool:
    """Return whether ``value`` is in the deployment's Textract endpoint set."""
    return value in TEXTRACT_REGIONS
