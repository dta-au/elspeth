# Azure AI Search RAG retrieval

Retrieves ranked context for each question from an existing Azure AI Search
index with the `azure_ai_search` transform. It does not
build the index and it does not generate an answer: populate the index with
Azure's own indexer, and add an `llm` transform after retrieval when an answer
is required (see [`chroma_rag_qa`](../chroma_rag_qa/) for that shape).

## Run

```bash
export AZURE_SEARCH_ENDPOINT=https://<service>.search.windows.net
export AZURE_SEARCH_INDEX=<index>
export AZURE_SEARCH_API_KEY=<query key>
elspeth validate --settings examples/azure_search_rag/settings.yaml
elspeth run --settings examples/azure_search_rag/settings.yaml --execute
```

A query key is enough; the transform never writes to the index. Before the
first row the run counts the documents in the index, records that call in the
audit trail, and refuses to start against a missing or empty one.

## What the index must provide

| Option | Default | The index must have |
| --- | --- | --- |
| `field_content` | `content` | a retrievable string field holding the chunk text |
| `field_id` | `id` | a retrievable key field |
| `field_title`, `field_url` | unset | optional retrievable strings, emitted as `source_name` and `source_link` in each source's metadata |
| `field_vector` | `contentVector` | for `vector` and `hybrid`: a vector field **with an integrated vectorizer** |
| `semantic_config` | unset | for `semantic`: the name of a semantic configuration |
| `select` | unset | optional list of fields to return; must include every field a `field_*` option names |
| `filter` | unset | optional OData `$filter`, sent verbatim |

The settings here use the field names the portal's "Import and vectorize data"
wizard creates (`chunk`, `chunk_id`, `title`, `text_vector`). The options are
spelled `field_*` because ELSPETH reads any option ending in `_field` as a row
column the transform consumes, and these name index fields. A hit whose
`field_content` or `field_id` field is absent is recorded as a skip with its reason;
if every hit is skipped the row reports `no_results`, so wrong field names show
up as `skipped_reasons: missing_content` rather than as an empty index.

`vector` and `hybrid` send the question as text (`vectorQueries[].kind: text`)
and Azure embeds it, so ELSPETH makes no embedding call and an index without a
vectorizer rejects the query. Set `select` on any index that stores vectors:
without it every retrievable field, vectors included, lands in
`policy__rag_sources`.

## Scores

`min_score` applies to a 0-1 score normalized per mode:

| `search_mode` | Azure score | Normalized over |
| --- | --- | --- |
| `vector` | `@search.score` | 0 - 1 |
| `hybrid` | `@search.score` (RRF) | 0 - 2/61, the ceiling for one text and one vector query |
| `semantic` | `@search.rerankerScore` | 0 - 4 |
| `keyword` | `@search.score` (BM25) | 0 - 50, a clamp: BM25 has no upper limit |

Ranges follow
[Hybrid search scoring](https://learn.microsoft.com/en-us/azure/search/hybrid-search-ranking).
A hybrid score measures rank agreement, not similarity, so tune `min_score`
against real queries before relying on it.

## Managed identity

```yaml
options:
  query_field: question
  output_prefix: policy
  endpoint: https://<service>.search.windows.net
  index: <index>
  use_managed_identity: true
  client_id: <client id>   # user-assigned identity; omit for system-assigned
```

The identity needs the `Search Index Data Reader` role on the search service,
and the service must have role-based access enabled. The transform uses
`ManagedIdentityCredential` only, so it never falls back to environment
service-principal variables or a developer login.

## Web-authored pipelines

A web-authored pipeline selects an operator profile and never names an
endpoint: the operator declares each search service in
`ELSPETH_WEB__AZURE_SEARCH_PROFILES` (endpoint, authentication and the indexes
web authors may query), and the author writes `profile: <alias>` and an `index`
the profile lists. `endpoint`, `api_key`, `use_managed_identity`, `client_id`
and `api_version` are refused in a web-authored pipeline. See
[Environment variables](../../docs/reference/environment-variables.md).
