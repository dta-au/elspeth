// data.js — sample content for the ELSPETH Web Composer UI kit. Plain globals.
window.ELSPETH_KIT = {
  user: { name: "Demo User", id: "demo" },

  // Plugin catalog entries grouped by family.
  catalog: {
    sources: [
      { name: "CSV", kind: "csv", type: "source", description: "Read rows from a CSV file. Headers normalized to identifiers at the boundary.", audit: [{ label: "strict parsing", tone: "positive" }] },
      { name: "Azure Blob", kind: "azure_blob", type: "source", description: "Stream rows from an Azure Blob container. Malformed rows are quarantined with an audit record.", audit: [{ label: "strict parsing", tone: "positive" }, { label: "quarantine on error", tone: "informational" }] },
      { name: "Dataverse", kind: "dataverse", type: "source", description: "Query rows from a Microsoft Dataverse table.", audit: [{ label: "typed schema", tone: "positive" }] },
      { name: "JSON", kind: "json", type: "source", description: "Read records from a JSON or JSONL file.", audit: [] },
      { name: "Chroma", kind: "chroma", type: "source", description: "Retrieve documents from a Chroma vector store for RAG.", audit: [{ label: "provenance tracked", tone: "informational" }] },
    ],
    transforms: [
      { name: "LLM query", kind: "llm", type: "transform", description: "Azure OpenAI / OpenRouter query with provider pooling and multi-query.", audit: [{ label: "fingerprinted secrets", tone: "positive" }, { label: "rate-limited", tone: "attention" }] },
      { name: "Field mapper", kind: "field_mapper", type: "transform", description: "Rename, drop, and remap fields with contract re-typing.", audit: [{ label: "contract-checked", tone: "positive" }] },
      { name: "Content Safety", kind: "azure_content_safety", type: "transform", description: "Azure Content Safety classification at the LLM boundary.", audit: [{ label: "zero-trust boundary", tone: "informational" }] },
      { name: "Prompt Shield", kind: "prompt_shield", type: "transform", description: "Detect prompt-injection attempts on external input.", audit: [{ label: "attention surfaced", tone: "attention" }] },
      { name: "Threshold gate", kind: "gate", type: "gate", description: "Pure-config gate: route rows by a named expression.", audit: [{ label: "reviewable config", tone: "positive" }] },
      { name: "Batch metrics", kind: "batch_classifier_metrics", type: "aggregation", description: "Local, audit-attributable classifier metrics over a batch.", audit: [{ label: "deterministic", tone: "positive" }] },
    ],
    sinks: [
      { name: "CSV out", kind: "csv", type: "sink", description: "Write rows to a CSV file with restored display headers.", audit: [{ label: "headers restored", tone: "informational" }] },
      { name: "Review queue", kind: "review_queue", type: "sink", description: "Route flagged rows to a human review queue.", audit: [{ label: "human-in-loop", tone: "informational" }] },
      { name: "Database", kind: "database", type: "sink", description: "Insert rows into a relational table.", audit: [{ label: "transactional", tone: "positive" }] },
      { name: "Azure Blob out", kind: "azure_blob", type: "sink", description: "Write artifacts to an Azure Blob container.", audit: [] },
    ],
  },

  // The pipeline the assistant "builds" — revealed node-by-node.
  pipeline: [
    { id: "src", type: "source", label: "csv", title: "Submissions", sub: "source · csv" },
    { id: "llm", type: "transform", label: "llm", title: "Classify", sub: "transform · llm" },
    { id: "gate", type: "gate", label: "gate", title: "Safety gate", sub: "gate · risk_score > 0.8" },
    { id: "ok", type: "sink", label: "csv", title: "Approved", sub: "sink · csv" },
    { id: "review", type: "sink", label: "queue", title: "Review queue", sub: "sink · review_queue" },
  ],

  yaml: `source:
  plugin: csv
  on_success: validated
  options:
    path: data/submissions.csv

transforms:
- name: classify
  plugin: llm
  input: validated
  on_success: classified
  options:
    prompt: "Classify the submission for abusive content."

gates:
- name: safety_gate
  input: classified
  condition: "row['risk_score'] > 0.8"
  routes:
    "true": review
    "false": approved

sinks:
  approved:
    plugin: csv
    options: { path: output/approved.csv }
  review:
    plugin: review_queue

landscape:
  url: sqlite:///./audit.db`,
};
