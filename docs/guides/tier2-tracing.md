# Tier 2 Plugin Tracing Guide

Tier 2 tracing provides deep LLM observability beyond the framework's Tier 1 telemetry. While Tier 1 captures latency, status, and content hashes for ALL external calls, Tier 2 captures full prompts, responses, and token-level metrics for LLM calls specifically.

> **Langfuse SDK v5.2+ Required**
>
> ELSPETH uses Langfuse SDK v5, built on OpenTelemetry standards. In a source
> checkout, install the locked tracing extra:
> ```bash
> uv sync --frozen --extra tracing-langfuse
> ```
> Do not install a second, unpinned SDK into the checkout environment.

Langfuse SDK 5 requires Langfuse Cloud or **self-hosted Langfuse 4.59.0 or
newer**. Older self-hosted v3 and v4 servers are unsupported; upgrade the
server before upgrading ELSPETH. The SDK also JSON-encodes metadata span
attributes, including string values. Custom OpenTelemetry exporters that
read those attributes must decode their JSON values. See
[Langfuse's SDK upgrade guide](https://langfuse.com/docs/observability/sdk/upgrade-path/python-v4-to-v5).

## Overview

ELSPETH has two independent telemetry tiers:

| Aspect | Tier 1 (Framework) | Tier 2 (Plugin) |
|--------|-------------------|-----------------|
| **Scope** | All external calls (LLM, HTTP, SQL) | LLM calls only |
| **Data captured** | Hashes, latency, status, call counts | Full prompts, responses, token usage |
| **Destination** | Generic observability platforms | LLM-specific platforms |
| **Configuration** | `telemetry:` section in settings | Plugin `options.tracing:` block |
| **Dependency** | Optional exporters | Optional SDK per provider |

**Key principle:** Tier 1 and Tier 2 are complementary. Use Tier 1 for operational monitoring and alerting. Use Tier 2 for prompt engineering, cost tracking, and LLM-specific analytics.

## Supported Providers

### Real-Time Plugins

| Provider | llm (provider: azure) | llm (provider: azure, queries: ...) | llm (provider: openrouter) | llm (provider: openrouter, queries: ...) |
|----------|----------------------|-------------------------------------|---------------------------|----------------------------------------|
| Azure AI (App Insights) | Yes | Yes | No | No |
| Langfuse | Yes | Yes | Yes | Yes |

**Why Azure AI doesn't work with OpenRouter:**
Azure AI tracing auto-instruments the OpenAI SDK. OpenRouter plugins use HTTP directly via `httpx`, so there's no SDK to instrument. Use Langfuse for OpenRouter tracing.

## Azure AI (Application Insights)

Azure AI tracing uses Azure Monitor OpenTelemetry to auto-instrument the OpenAI SDK. This captures full prompts and responses in Application Insights.

### Configuration

```yaml
transforms:
  - plugin: llm
    options:
      provider: azure
      deployment_name: gpt-4o
      endpoint: ${AZURE_OPENAI_ENDPOINT}
      api_key: ${AZURE_OPENAI_KEY}
      template: |
        Analyze: {{ row.text }}

      # Tier 2: Azure AI tracing
      tracing:
        provider: azure_ai
        connection_string: ${APPLICATIONINSIGHTS_CONNECTION_STRING}
        enable_content_recording: true  # Capture prompts/responses (default: true)
        enable_live_metrics: false      # Live Metrics Stream (default: false)
```

### Required Dependency

```bash
uv sync --frozen --extra azure
# azure-monitor-opentelemetry is bundled with the azure extras
```

### Finding Your Connection String

1. Go to Azure Portal > Application Insights resource
2. Overview > Connection String (copy)
3. Set as environment variable:
   ```bash
   : "${APPLICATIONINSIGHTS_CONNECTION_STRING:?load the connection string from the secret store}"
   ```

### What Gets Captured

With `enable_content_recording: true`:
- Full prompt messages (system + user)
- Full response content
- Token usage (prompt_tokens, completion_tokens, total_tokens)
- Latency metrics
- Model/deployment information
- Error details on failure

### Process-Level Warning

Azure Monitor OpenTelemetry configures at the process level. If multiple plugins configure `azure_ai` tracing, the first one to initialize wins. All subsequent configurations are ignored.

## Langfuse

Langfuse provides LLM-specific observability with prompt engineering analytics, cost tracking, and evaluation capabilities.

### Configuration

```yaml
transforms:
  - plugin: llm  # With any provider (azure, openrouter, etc.)
    options:
      provider: azure
      deployment_name: gpt-4o
      endpoint: ${AZURE_OPENAI_ENDPOINT}
      api_key: ${AZURE_OPENAI_KEY}
      template: |
        Analyze: {{ row.text }}

      # Tier 2: Langfuse tracing
      tracing:
        provider: langfuse
        public_key: ${LANGFUSE_PUBLIC_KEY}
        secret_key: ${LANGFUSE_SECRET_KEY}
        host: https://cloud.langfuse.com  # Default, or self-hosted URL
        tracing_enabled: true  # v3: Can be disabled per-plugin (default: true)
```

### Host Constraints

ELSPETH sends `public_key` and `secret_key` to `host`, so the host is a
credential-egress destination and is validated fail-closed when settings load.
A pipeline whose `host` breaks any of these rules refuses to start:

| Rule | Rejected example |
| --- | --- |
| The scheme must be `https` | `http://langfuse.example.com` |
| The URL must not carry embedded credentials | `https://user:pass@langfuse.example.com` |
| The URL must resolve to a hostname | `https://`, `langfuse.example.com` |

**Loopback exception.** Plaintext `http://` is permitted when — and only when —
the hostname is a loopback address (`localhost`, `127.0.0.0/8`, `::1`), so a
self-hosted Langfuse running on the same machine during development works
without a certificate:

```yaml
        host: http://localhost:3000  # Permitted: loopback only
```

A non-loopback hostname over `http://` is refused even on a private network,
because plaintext transport exposes both API keys to anything on the path.

### Required Dependency

```bash
uv sync --frozen --extra tracing-langfuse
```

> **Note:** Langfuse SDK v5.2+ is required and pinned by `uv.lock`. This
> version uses OpenTelemetry-based context managers for trace lifecycle
> management and is thread-safe for concurrent pipeline execution.

The Web Composer's operator telemetry custody depends on reviewed
OpenTelemetry SDK internals. ELSPETH 0.8.12 locks the SDK and OTLP exporters
to 1.45.2 and the Prometheus exporter to 0.66b1. Keep those exact versions
together when installing the web interface; its startup check refuses an
unreviewed SDK or exporter version. Existing Langfuse `host`, credential and
tracing configuration fields remain supported.

### Langfuse Credentials

1. Sign up at [cloud.langfuse.com](https://cloud.langfuse.com) (or deploy self-hosted)
2. Create a project
3. Go to Settings > API Keys
4. Copy Public Key and Secret Key
5. Set as environment variables:
   ```bash
   : "${LANGFUSE_PUBLIC_KEY:?load the public key from the secret store}"
   : "${LANGFUSE_SECRET_KEY:?load the secret key from the secret store}"
   ```

### What Gets Captured

For real-time plugins (llm with provider: azure, llm with provider: openrouter, etc.):
- Full prompt content
- Full response content
- Token usage (input, output, total)
- Latency per call
- Model information
- Token ID for correlation with Landscape

For multi-query plugins (llm with provider: azure and queries, llm with provider: openrouter and queries):
- Trace per row (not per query)
- Aggregate query count and success count
- Total latency for all queries in the row

For batch plugins:
- Azure Batch: Job-level trace (submit/complete events, not per-row)
- OpenRouter Batch: Per-call traces (full observability)

### Langfuse Advantages

Unlike Azure AI auto-instrumentation:
- **Per-instance configuration:** Each plugin can have different Langfuse settings
- **Works with any plugin:** HTTP-based manual instrumentation, not SDK-dependent
- **Self-hosted option:** Keep data on-premises if required
- **LLM-specific features:** Prompt versioning, A/B testing, evaluation scores
- **Thread-safe:** Uses OpenTelemetry thread-local context (safe for concurrent pipelines)

### Langfuse observations

ELSPETH records Langfuse observations after the call completes. A successful
observation carries the prompt, response, model and token usage. The tracer
also supports error observations with the prompt, model and error status;
an error recorded by a plugin produces an error generation. Landscape remains
the authoritative audit trail when optional tracing is unavailable.

## Using Both Tiers Together

Tier 1 and Tier 2 serve different purposes and can (and should) be used together:

```yaml
# Tier 1: Framework telemetry to Datadog for operational monitoring
telemetry:
  enabled: true
  granularity: rows
  exporters:
    - name: datadog
      options:
        service_name: my-pipeline
        env: production

# Tier 2: LLM-specific tracing to Langfuse for prompt engineering
transforms:
  - plugin: llm
    options:
      provider: azure
      deployment_name: gpt-4o
      endpoint: ${AZURE_OPENAI_ENDPOINT}
      api_key: ${AZURE_OPENAI_KEY}
      template: |
        Analyze: {{ row.text }}
      tracing:
        provider: langfuse
        public_key: ${LANGFUSE_PUBLIC_KEY}
        secret_key: ${LANGFUSE_SECRET_KEY}
```

**Workflow with both tiers:**

1. **Datadog alert fires:** "LLM latency P95 > 5s"
2. **Datadog trace shows:** `run_id=run-abc123`, `node_id=llm_classifier`
3. **Landscape query:** Get failed row IDs and error messages
4. **Langfuse analysis:** Review actual prompts, identify why responses were slow
5. **Root cause:** Prompts too long, hitting context limit

## Tier 1 + Tier 2 Azure AI Conflict

**Warning:** Using Azure AI for BOTH Tier 1 telemetry AND Tier 2 plugin tracing can cause conflicts.

### The Problem

Both systems use OpenTelemetry:
- Tier 1 `azure_monitor` exporter configures OTEL for general telemetry
- Tier 2 `azure_ai` tracing configures OTEL for LLM auto-instrumentation

OpenTelemetry's global tracer provider can only be set once. The second configuration may fail silently or overwrite the first.

### Detection

If you see this log warning:
```
Existing OpenTelemetry tracer detected - Azure AI tracing may conflict with Tier 1 telemetry
```

### Solutions

**Option 1: Use Langfuse for Tier 2 (Recommended)**
```yaml
# Tier 1: Azure Monitor for framework telemetry
telemetry:
  exporters:
    - name: azure_monitor
      options:
        connection_string: ${APPLICATIONINSIGHTS_CONNECTION_STRING}

# Tier 2: Langfuse for LLM tracing (no OTEL conflict)
transforms:
  - plugin: llm
    options:
      provider: azure
      tracing:
        provider: langfuse
        public_key: ${LANGFUSE_PUBLIC_KEY}
        secret_key: ${LANGFUSE_SECRET_KEY}
```

**Option 2: Use only Tier 2 Azure AI**
```yaml
# Skip Tier 1 azure_monitor exporter
telemetry:
  exporters:
    - name: datadog  # Use non-OTEL exporter
      options:
        service_name: my-pipeline

# Tier 2: Azure AI (now owns OTEL)
transforms:
  - plugin: llm
    options:
      provider: azure
      tracing:
        provider: azure_ai
        connection_string: ${APPLICATIONINSIGHTS_CONNECTION_STRING}
```

**Option 3: Use only Tier 1 with full granularity**
```yaml
# Tier 1 at 'full' granularity captures external call details
telemetry:
  granularity: full  # Includes ExternalCallCompleted events
  exporters:
    - name: azure_monitor
      options:
        connection_string: ${APPLICATIONINSIGHTS_CONNECTION_STRING}

# No Tier 2 tracing - rely on Tier 1 for LLM visibility
transforms:
  - plugin: llm
    options:
      provider: azure
      # No tracing: block
```

## Multi-Plugin Scenarios

When multiple LLM plugins have different tracing configurations:

| Plugin A Config | Plugin B Config | Behavior |
|-----------------|-----------------|----------|
| `langfuse` | `langfuse` (same host) | Both trace to same project |
| `langfuse` (host A) | `langfuse` (host B) | Each traces to its own instance |
| `azure_ai` | `azure_ai` | First wins (process-level) |
| `azure_ai` | `langfuse` | Both work independently |
| `azure_ai` | `none` | Only Plugin A traces |
| `langfuse` | `none` | Only Plugin A traces |

**Best practice for multi-plugin pipelines:**
```yaml
transforms:
  # All LLM plugins trace to same Langfuse project
  - plugin: llm
    options:
      provider: azure
      tracing:
        provider: langfuse
        public_key: ${LANGFUSE_PUBLIC_KEY}
        secret_key: ${LANGFUSE_SECRET_KEY}

  - plugin: llm
    options:
      provider: openrouter
      tracing:
        provider: langfuse
        public_key: ${LANGFUSE_PUBLIC_KEY}
        secret_key: ${LANGFUSE_SECRET_KEY}
```

## Missing-dependency behavior

When Langfuse tracing is explicitly configured but the SDK is absent, startup
fails with `RuntimeError`; ELSPETH does not silently run without requested
tracing. In a source checkout, repair the locked environment and restart:

```bash
uv sync --frozen --extra tracing-langfuse
```

Runtime transport failures are logged and do not become Landscape evidence,
but programming errors still fail closed.

**To explicitly disable tracing:**
```yaml
transforms:
  - plugin: llm
    options:
      provider: azure
      # Simply omit the tracing block, or:
      tracing:
        provider: none
```

**To make missing SDK a hard error:**
There is no configuration for this. If you require tracing, ensure the dependency is installed in your environment. Consider adding a pre-flight check in your deployment pipeline.

## Privacy Considerations

### PII in Prompts and Responses

Tier 2 tracing captures full prompts and responses. This may include:
- Personal identifiable information (PII) from input data
- Sensitive business data
- Medical, financial, or legal information

**Mitigations:**

1. **Disable content recording (Azure AI only):**
   ```yaml
   tracing:
     provider: azure_ai
     connection_string: ${APPLICATIONINSIGHTS_CONNECTION_STRING}
     enable_content_recording: false  # Only capture metadata
   ```

2. **Use Langfuse with data masking:**
   Langfuse supports regex-based masking in the UI for viewing sensitive data.

3. **Self-hosted Langfuse:**
   Deploy Langfuse in your own infrastructure to keep data within your security boundary.

4. **No Tier 2 tracing:**
   Rely on Tier 1 telemetry (hashes only) and Landscape audit trail for investigations.

### Data Residency

| Provider | Data Location | Control |
|----------|---------------|---------|
| Azure AI (App Insights) | Your Azure subscription region | Full control |
| Langfuse Cloud | EU (default) or US | Choose region at signup |
| Langfuse Self-Hosted | Your infrastructure | Full control |

### Compliance Considerations

| Requirement | Recommendation |
|-------------|----------------|
| GDPR | Use EU-region Langfuse or self-hosted |
| HIPAA | Self-hosted Langfuse with BAA, or disable Tier 2 |
| SOC 2 | Azure AI (inherits Azure compliance) or self-hosted |
| Data minimization | `enable_content_recording: false` or no Tier 2 |

## Troubleshooting

### Tracing Not Appearing in Langfuse

**Check configuration:**
```yaml
tracing:
  provider: langfuse
  public_key: ${LANGFUSE_PUBLIC_KEY}  # Must resolve to actual key
  secret_key: ${LANGFUSE_SECRET_KEY}  # Must resolve to actual key
  host: https://cloud.langfuse.com    # Correct host URL
```

**Check environment variables:**
```bash
test -n "${LANGFUSE_PUBLIC_KEY:-}" && printf '%s\n' 'LANGFUSE_PUBLIC_KEY is set'
test -n "${LANGFUSE_SECRET_KEY:-}" && printf '%s\n' 'LANGFUSE_SECRET_KEY is set'
```

Do not print either key while troubleshooting.

**Check for validation errors in logs:**
```
Tracing configuration error, error="langfuse tracing requires public_key..."
```

**Verify SDK is installed:**
```bash
.venv/bin/python -c "import langfuse; print(langfuse.__version__)"
```

### Azure AI Tracing Not Working

**Check for OTEL conflict warning:**
```
Existing OpenTelemetry tracer detected - Azure AI tracing may conflict...
```

**Verify Azure Monitor package:**
```bash
.venv/bin/python -c "from azure.monitor.opentelemetry import configure_azure_monitor"
```

**Check connection string format:**
```bash
# Should start with "InstrumentationKey=" or "IngestionEndpoint="
echo $APPLICATIONINSIGHTS_CONNECTION_STRING
```

### Traces Not Correlating with Landscape

Tier 2 traces include `token_id` in metadata for correlation. To link a Langfuse trace to Landscape:

1. Find the trace in Langfuse
2. Look in the span metadata for `token_id` (Langfuse v3 uses W3C Trace Context IDs, not custom observation IDs)
3. Query Landscape:
   ```bash
   elspeth explain --run <run_id> --token <token_id>
   ```

**Note on Langfuse v3:** The SDK now uses W3C Trace Context compliant IDs. Custom observation IDs are no longer supported. Use `metadata.token_id` for correlation with the ELSPETH Landscape audit trail.

### High Latency from Tracing

**Symptoms:**
- Pipeline slows down after enabling Tier 2 tracing
- Timeout errors during Langfuse flush

**Solutions:**

1. **Check network latency to tracing endpoint:**
   ```bash
   curl -o /dev/null -s -w "%{time_total}\n" https://cloud.langfuse.com
   ```

2. **Use regional endpoint:**
   For Langfuse Cloud, use the endpoint in your region (EU or US).

3. **Consider self-hosted:**
   Deploy Langfuse in the same region as your pipeline.

4. **Reduce tracing scope:**
   Only enable Tier 2 for debugging, not production.

### Langfuse Flush Errors

**Symptoms:**
```
Failed to flush Langfuse tracing, error="..."
```

**Causes:**
- Network connectivity issues
- Invalid credentials
- Langfuse service unavailable

**Solutions:**
- Verify credentials
- Check Langfuse status page
- Errors are logged but don't crash the pipeline
