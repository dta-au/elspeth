#!/usr/bin/env bash
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
cache_dir="${UV_CACHE_DIR:-${TMPDIR:-/tmp}/elspeth-gateway-uv-cache-${UID}}"

export UV_CACHE_DIR="$cache_dir"

# Pass --upgrade or --upgrade-package <name> explicitly when an update is
# intended. Ordinary setup, CI, and image builds use --frozen/--check only.
uv lock --project "${repo_root}/gateway" "$@"
uv lock --check --project "${repo_root}/gateway"
