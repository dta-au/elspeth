"""Shared lazy AWS S3 client construction for optional AWS plugins."""

from __future__ import annotations

import os
import re
from typing import Any

_MAX_REGION_CHARS = 64
_AWS_REGION_PATTERN = re.compile(r"[A-Za-z0-9-]+\Z")


def _region_from_environment(name: str) -> str | None:
    if name not in os.environ:
        return None
    value = os.environ[name]
    if not value or len(value) > _MAX_REGION_CHARS or _AWS_REGION_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{name} must be a bounded AWS region identifier")
    return value


def _resolve_region_name(region_name: str | None) -> str | None:
    if region_name is not None:
        return region_name
    primary_region = _region_from_environment("AWS_REGION")
    default_region = _region_from_environment("AWS_DEFAULT_REGION")
    if primary_region is not None and default_region is not None and primary_region != default_region:
        raise ValueError("conflicting AWS region environment variables")
    if primary_region is not None:
        return primary_region
    return default_region


def build_s3_client(region_name: str | None, endpoint_url: str | None) -> Any:
    """Build an S3 client with bounded SDK timeouts and retry attempts."""
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:
        raise ImportError('boto3 is required for aws_s3 plugins; install Elspeth with the "aws" extra') from exc

    config = Config(
        connect_timeout=10,
        read_timeout=30,
        retries={"mode": "standard", "total_max_attempts": 3},
    )
    return boto3.client(
        "s3",
        region_name=_resolve_region_name(region_name),
        endpoint_url=endpoint_url,
        config=config,
    )
