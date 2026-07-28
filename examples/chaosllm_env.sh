#!/usr/bin/env bash
# Shared environment for local ChaosLLM examples.
#
# ELSPETH fingerprints secret-shaped configuration before writing it to the
# audit trail. The ChaosLLM token is fake, but it still occupies an api_key
# field, so clean checkouts need a fingerprint key. Generate one for this
# launcher process unless the operator already supplied a key or explicitly
# enabled the development-only raw-secret mode.

if [ -z "${ELSPETH_FINGERPRINT_KEY:-}" ] \
    && [ "${ELSPETH_ALLOW_RAW_SECRETS:-}" != "true" ]; then
    _ELSPETH_EXAMPLES_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    ELSPETH_FINGERPRINT_KEY="$(
        "$_ELSPETH_EXAMPLES_DIR/../.venv/bin/python" \
            -c 'import secrets; print(secrets.token_hex(32))'
    )"
    export ELSPETH_FINGERPRINT_KEY
    unset _ELSPETH_EXAMPLES_DIR
fi
