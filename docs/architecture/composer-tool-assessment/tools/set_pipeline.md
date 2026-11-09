# `set_pipeline` assessment

Baseline: `3b28d537be98234da8135c418bf185a998c18b68` — 0.8.9. Reconnaissance and design only.

## 1. Purpose and exposure

Atomically author a full pipeline replacement, including source(s), nodes, display edges, outputs and metadata. It is not a patch: omitted prior elements are not implicitly retained. The outer web tool accepts a pipeline envelope; internal canonical dispatch and MCP use the flat document. It is a mutation tool with argument-dependent blob-store effects for inline source content.

## 2. Contracts and authority

Exactly one non-null source or named sources map is required, plus nodes, edges and outputs arrays. Failure-routing choices are explicit; metadata defaults are freshly constructed rather than inherited. Node connection fields are runtime wiring authority; edges are advisory display records. Raw credentials, invented source custody and user-authored resolved review metadata are prohibited. Input shape, per-component semantic acceptance, whole-graph validity, candidate acceptability and committed publication are distinct states ([src/elspeth/web/composer/tools/sessions.py:870](../../../../src/elspeth/web/composer/tools/sessions.py#L870), [src/elspeth/web/composer/tools/sessions.py:2113](../../../../src/elspeth/web/composer/tools/sessions.py#L2113), [src/elspeth/web/composer/tools/sessions.py:2355](../../../../src/elspeth/web/composer/tools/sessions.py#L2355)).

`SetPipelineCandidate.acceptable` requires both success and valid validation ([src/elspeth/web/composer/tools/sessions.py:587](../../../../src/elspeth/web/composer/tools/sessions.py#L587)); direct handler success can describe an incomplete authored draft. The handler creates version+1 in memory, while proposal/operation writers own persistent publication. Inline creation has additional durable blob custody; recovery cannot be assumed to undo it.

## 3. Source trace to the user

1. Exposure and wire decoding: canonical declaration → web pipeline envelope → closed semantic admission ([src/elspeth/web/composer/tools/wire_projection.py:1](../../../../src/elspeth/web/composer/tools/wire_projection.py#L1), [src/elspeth/web/composer/tool_batch.py:1399](../../../../src/elspeth/web/composer/tool_batch.py#L1399)).
2. Candidate construction: argument model → explicit failure choices → policy/plugin admission → source/path/blob ownership → node option/review/expression/barrier checks → output secret/path/file-collision checks ([src/elspeth/web/composer/tools/sessions.py:652](../../../../src/elspeth/web/composer/tools/sessions.py#L652), [src/elspeth/web/composer/tools/sessions.py:999](../../../../src/elspeth/web/composer/tools/sessions.py#L999), [src/elspeth/web/composer/tools/sessions.py:1275](../../../../src/elspeth/web/composer/tools/sessions.py#L1275), [src/elspeth/web/composer/tools/sessions.py:1495](../../../../src/elspeth/web/composer/tools/sessions.py#L1495), [src/elspeth/web/composer/tools/sessions.py:1720](../../../../src/elspeth/web/composer/tools/sessions.py#L1720)).
3. Construct immutable specs and version+1 state; validate structural invariants and reconcile authoritative reviews; reject unknown/forged review bindings ([src/elspeth/web/composer/tools/sessions.py:1855](../../../../src/elspeth/web/composer/tools/sessions.py#L1855), [src/elspeth/web/composer/tools/sessions.py:1959](../../../../src/elspeth/web/composer/tools/sessions.py#L1959)).
4. Direct dispatch may persist prepared inline blob bytes after candidate construction and report quota failure ([src/elspeth/web/composer/tools/sessions.py:2057](../../../../src/elspeth/web/composer/tools/sessions.py#L2057)). The freeform path can prevalidate/finalize a candidate before explicit proposal custody or automatic application ([src/elspeth/web/composer/tool_batch.py:1748](../../../../src/elspeth/web/composer/tool_batch.py#L1748), [src/elspeth/web/composer/tool_batch.py:1882](../../../../src/elspeth/web/composer/tool_batch.py#L1882)).
5. Explicit approval creates a proposal; accept revalidates exact canonical authority via `prepare_pipeline_proposal_commit`, while auto-commit intent settles through the shared route owner ([src/elspeth/web/composer/pipeline_commit.py:364](../../../../src/elspeth/web/composer/pipeline_commit.py#L364), [src/elspeth/web/sessions/routes/composer/proposals.py:347](../../../../src/elspeth/web/sessions/routes/composer/proposals.py#L347), [src/elspeth/web/sessions/composer_turn.py:1132](../../../../src/elspeth/web/sessions/composer_turn.py#L1132)).
6. Audit binds canonical authored arguments and resulting version; ToolCallCard shows pending/committed/rejected proposal state, while graph and readiness reflect settled state ([src/elspeth/web/composer/tool_batch.py:1470](../../../../src/elspeth/web/composer/tool_batch.py#L1470), [src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx:147](../../../../src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx#L147)).

## 4. Branches, failures and recovery

Branches requiring separate regression: singular path source; existing blob source; inline literal source; named sources; trusted reviewed source rehydration; source/blob ambiguity; missing storage context; quota exhaustion; policy refusal; malformed options; failure-route omission; gate expression/routes; queue/row_union/collector contracts; secret references; file-sink collision policy; review reconciliation; invalid but authored draft; acceptable candidate; explicit proposal; automatic commit; stale acceptance; rejected proposal; cancellation after blob work and before graph settlement. Candidate failure preserves prior graph; complete replacement intentionally resets omitted metadata to defaults. Component rejection accumulation skips dependent checks after a local normalization failure rather than emitting speculative downstream errors ([src/elspeth/web/composer/tools/sessions.py:1283](../../../../src/elspeth/web/composer/tools/sessions.py#L1283)).

**Cache, idempotency, concurrency and retry:** `ToolKind.MUTATION`, `cacheable=False`. Repeating the same public arguments is not guaranteed idempotent: construction assigns current version+1 and inline content may allocate/persist new blob custody. No caller idempotency token is part of this tool schema. Reuse a proposal/operation's durable identity for recovery rather than issuing a new authoring call after uncertain delivery. The synchronous handler has no automatic retry loop. The proposal commit path checks exact pending status, base state ID/content hash and operation fence before work (`pipeline_commit.py:364–395`); stale concurrent changes therefore require reinspection/reproposal, not blind replay. Provider request retries, proposal acceptance retries and new set_pipeline calls have distinct authorities and effects.

## 5. Defects, restrictions and hypotheses

No new set_pipeline behavioral defect was demonstrated in this lane. **Structural observation:** candidate construction owns substantial domain logic for sources, transforms, outputs, secrets and interpretation metadata, while batch/commit paths also perform finalization and custody. That makes public-tool parity and authority-order review necessary; file size alone is not a defect. **Restriction:** exact historical reconstruction is not guaranteed; read-before-rebuild and preserve/refuse remain required. Any source-owned required-control insertion must remain validation/admission of provider-authored structure, within the Composer invariant.

## 6. Ownership, remediation and dependencies

Orchestrator owns proposal/commit lifecycle and exact state authority; candidate builder owns a deterministic candidate; source/blob, plugin, graph, review and sink policy owners own their respective validation. Prefer explicit candidate/effect preparation and one settlement boundary over another server synthesis path. Before moving code, define which transformations are normalization, required-control admission or planner authorship and prove each transition still invokes the provider where required. Dependency order: exact authority/result contracts → candidate parity → lifecycle settlement → public tool migration. Preserve current data; no migration/reset is authorized.

**Implementation risk:** High: changes span graph replacement, review binding and blob/proposal settlement. Preserve current bytes/authority and separately test cancellation before and after each durable effect.

## 7. Evidence, regression checks and limits

Read full replacement/version tests and invalid-gate preservation tests ([tests/unit/web/composer/tools/test_tools_pipeline.py:196](../../../../tests/unit/web/composer/tools/test_tools_pipeline.py#L196), [tests/unit/web/composer/tools/test_tools_pipeline.py:1842](../../../../tests/unit/web/composer/tools/test_tools_pipeline.py#L1842)); inspected argument/source/review guards in production. Entire test_tools_pipeline.py passed within the 370-test selection. This proves the covered handler/dispatch branches, not deployment settlement or PostgreSQL races. Confidence: high in described ownership/branch structure, moderate in exhaustive safety. Required later checks: explicit/automatic approval parity, inline blob rejection leaves no unauthorized publication, stale proposed state cannot commit, cancellation preserves actual effects and audit, full replacement and incremental construction yield equivalent runtime wiring.

Source assessment is pinned to `3b28d537be98234da8135c418bf185a998c18b68` (0.8.9). The forthcoming 0.8.10-lite tree must be re-read at these boundaries; this sheet makes no prediction about it. Source inspection and selected local tests do not establish live provider behavior, deployed persistence acceptance, PostgreSQL contention or browser rendering. No production code was changed.

## Runtime argument schema and measured exposure

Synchronous kind: `mutation`. Declaration cache opt-in: `false`. Handler: [`elspeth.web.composer.tools.sessions._handle_set_pipeline`](../../../../src/elspeth/web/composer/tools/sessions.py#L2097).

| Surface | Exposed |
|---|---|
| Canonical emitter | yes |
| Freeform loop | yes |
| Planner default | no |
| Composer MCP | yes |

Both measured provider dialects have the same name membership. Wire schemas, required-null projection, phase/policy filtering and available dependencies can still differ; exposure does not certify execution readiness.

This is the runtime-emitted canonical input schema, before provider wire projection. Public dispatch closes the root object where necessary; owned argument models and kind-specific/domain guards can impose additional constraints described above. Dynamic plugin options are governed by the selected plugin schema and policy projection, not by arbitrary object permissiveness here.

```json
{
  "type": "object",
  "properties": {
    "source": {
      "type": "object",
      "additionalProperties": false,
      "description": "Source configuration. Use blob_id to bind an already uploaded session blob, or inline_blob to materialize user-provided literal data atomically with the pipeline. null carries NO meaning — it is not 'keep the existing source'. Every call must supply source or sources as a real object: on a full rebuild, re-supply the complete existing source block, including blob_id only when that block carries one.",
      "properties": {
        "plugin": {
          "type": "string"
        },
        "blob_id": {
          "type": [
            "string",
            "null"
          ],
          "description": "Existing ready session blob ID to bind as this source (resolved exactly like set_source_from_blob)."
        },
        "options": {
          "type": "object",
          "description": "Plugin-specific source config. Required by most file/data sources. Server/resolver-owned source option roots are not settable: blob_ref, blobs, source_authoring. Bind blobs through set_source_from_blob, set_source_from_blobs, source.blob_id, or source.inline_blob. Inside interpretation_requirements, only kind, user_term, and draft, with optional display_title (a non-empty string of at most 200 characters) are authorable. display_title is a concise human-readable approval name. Resolver-owned fields are not settable: accepted_artifact_hash, accepted_value, event_id, id, resolved_prompt_template_hash, status. Omit those fields. On incremental surfaces, persist an authorable pending review with the offered mutation tool and call request_interpretation_review when offered. On a proposal-only surface, include the pending requirements in the appropriate source/node options of the complete proposal instead. Backend-owned review kinds are surfaced automatically. The user resolves the card and ELSPETH writes resolved review metadata."
        },
        "on_success": {
          "type": "string",
          "description": "Connection-name string the source PUBLISHES. Some downstream consumer (node 'input' or output 'sink_name') MUST equal this. Connections match by string, not by node id."
        },
        "on_validation_failure": {
          "type": "string",
          "description": "Required explicit non-empty source-validation failure choice: 'discard' or an eligible named sink. Any other value, including 'quarantine', must match a configured output/sink name. Full replacement does not reuse prior choices; null/empty is invalid."
        },
        "description": {
          "type": [
            "string",
            "null"
          ],
          "description": "One short sentence of plain prose saying what this step does, shown to reviewers on the Spec tab. Supply it when creating the step and refresh it whenever you change what the step does. Informational only — never affects validation or execution."
        },
        "inline_blob": {
          "type": [
            "object",
            "null"
          ],
          "additionalProperties": false,
          "description": "Inline source content to create as a session blob before binding. Fields mirror create_blob.",
          "properties": {
            "filename": {
              "type": "string"
            },
            "mime_type": {
              "type": "string",
              "enum": [
                "application/json",
                "application/jsonl",
                "application/x-jsonlines",
                "text/csv",
                "text/jsonl",
                "text/plain"
              ]
            },
            "content": {
              "type": "string",
              "maxLength": 262144
            },
            "description": {
              "type": [
                "string",
                "null"
              ]
            }
          },
          "required": [
            "filename",
            "mime_type",
            "content"
          ]
        }
      },
      "required": [
        "plugin",
        "on_success",
        "on_validation_failure"
      ]
    },
    "sources": {
      "type": "object",
      "description": "Named source roots keyed by stable source name; use instead of source for multi-source pipelines. Values share source's shape minus blob_id/inline_blob. null carries NO meaning; every call must supply source or sources as a real object.",
      "additionalProperties": {
        "type": "object",
        "additionalProperties": false,
        "properties": {
          "plugin": {
            "type": "string"
          },
          "options": {
            "type": "object",
            "description": "Plugin-specific source config. Server/resolver-owned source option roots are not settable: blob_ref, blobs, source_authoring. Bind blobs through set_source_from_blob, set_source_from_blobs, source.blob_id, or source.inline_blob. Inside interpretation_requirements, only kind, user_term, and draft, with optional display_title (a non-empty string of at most 200 characters) are authorable. display_title is a concise human-readable approval name. Resolver-owned fields are not settable: accepted_artifact_hash, accepted_value, event_id, id, resolved_prompt_template_hash, status. Omit those fields. On incremental surfaces, persist an authorable pending review with the offered mutation tool and call request_interpretation_review when offered. On a proposal-only surface, include the pending requirements in the appropriate source/node options of the complete proposal instead. Backend-owned review kinds are surfaced automatically. The user resolves the card and ELSPETH writes resolved review metadata."
          },
          "on_success": {
            "type": "string"
          },
          "on_validation_failure": {
            "type": "string",
            "description": "Required explicit non-empty source-validation failure choice: 'discard' or an eligible named sink. Any other value, including 'quarantine', must match a configured output/sink name. Full replacement does not reuse prior choices; null/empty is invalid."
          },
          "description": {
            "type": [
              "string",
              "null"
            ],
            "description": "One short sentence of plain prose saying what this step does, shown to reviewers on the Spec tab. Supply it when creating the step and refresh it whenever you change what the step does. Informational only — never affects validation or execution."
          }
        },
        "required": [
          "plugin",
          "on_success",
          "on_validation_failure"
        ]
      }
    },
    "nodes": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "id": {
            "type": "string"
          },
          "node_type": {
            "type": "string"
          },
          "plugin": {
            "type": [
              "string",
              "null"
            ]
          },
          "input": {
            "type": "string",
            "description": "Upstream published connection this node consumes, normally an on_success or gate route. Valid implicit aggregation/queue/coalesce publications can equal the publisher's node id. For coalesce/row_union use their branch/input rules. Error routes cannot feed arbitrary processing nodes; only a permitted enclosing bound-region closer is eligible."
          },
          "on_success": {
            "type": [
              "string",
              "null"
            ],
            "description": "Connection-name string this node PUBLISHES; some downstream input/sink_name MUST equal it. Omit for gates (they route via condition+routes). A coalesce publishes under its own node id, and its on_success, when set, may ONLY name a sink. A row_union's on_success must name a downstream processing connection, never a sink."
          },
          "on_error": {
            "type": [
              "string",
              "null"
            ],
            "minLength": 1,
            "description": "Node-level error policy (transform/aggregation/gate): 'discard' or a declared sink name. A transform or gate inside a valid bound region may instead divert loss to that enclosing region's closer; aggregations cannot. Arbitrary processing inputs are forbidden. Transforms/aggregations require an explicit non-empty choice; full replacement does not reuse prior choices. For a gate it covers row expression-evaluation errors and is authored on the node, never as an edge; omit it to preserve fail-fast behavior. A failed aggregation batch applies this policy to every buffered row; discard records quarantine outcomes."
          },
          "options": {
            "type": "object",
            "description": "Plugin-specific node config. Row-column options use the field name carried by upstream rows, normally a normalized header or source field_mapping target. Runtime-owned LLM option fields are not settable: approved_prompt_artifact_hash. Omit them; ELSPETH re-derives them during review reconciliation or execution. Inside interpretation_requirements, only kind, user_term, and draft, with optional display_title (a non-empty string of at most 200 characters) are authorable. display_title is a concise human-readable approval name. Resolver-owned fields are not settable: accepted_artifact_hash, accepted_value, event_id, id, resolved_prompt_template_hash, status. Omit those fields. On incremental surfaces, persist an authorable pending review with the offered mutation tool and call request_interpretation_review when offered. On a proposal-only surface, include the pending requirements in the appropriate source/node options of the complete proposal instead. Backend-owned review kinds are surfaced automatically. The user resolves the card and ELSPETH writes resolved review metadata."
          },
          "condition": {
            "type": [
              "string",
              "null"
            ]
          },
          "routes": {
            "type": [
              "object",
              "null"
            ],
            "additionalProperties": {
              "type": "string"
            },
            "description": "Gate route mapping to sink names, downstream connection names, 'fork', or 'discard' for an audited terminal drop."
          },
          "fork_to": {
            "type": [
              "array",
              "null"
            ],
            "items": {
              "type": "string"
            }
          },
          "branches": {
            "type": [
              "array",
              "object",
              "null"
            ],
            "items": {
              "type": "string"
            },
            "additionalProperties": {
              "type": "string"
            },
            "description": "Branch inputs for coalesce or row_union. For row_union, every branches value is a real consumed input connection; input must repeat the first value only as an adapter placeholder."
          },
          "policy": {
            "type": [
              "string",
              "null"
            ]
          },
          "merge": {
            "type": [
              "string",
              "null"
            ]
          },
          "trigger": {
            "type": [
              "object",
              "null"
            ],
            "properties": {
              "count": {
                "type": [
                  "integer",
                  "null"
                ]
              },
              "timeout_seconds": {
                "type": [
                  "number",
                  "null"
                ]
              },
              "condition": {
                "type": [
                  "string",
                  "null"
                ]
              }
            },
            "additionalProperties": false
          },
          "output_mode": {
            "type": [
              "string",
              "null"
            ],
            "description": "Aggregation mode: transform (default), or passthrough only when the plugin's catalogue aggregation_output_modes includes it; passthrough requires one emitted row per buffered row."
          },
          "expected_output_count": {
            "type": [
              "integer",
              "null"
            ],
            "description": "Expected aggregation output row count for output_mode='transform' (the default); omit for 'passthrough'. Also omit when output count depends on group_by distinct values."
          },
          "timeout_seconds": {
            "type": [
              "number",
              "null"
            ],
            "exclusiveMinimum": 0,
            "description": "Optional finite positive timeout for structural barrier nodes (coalesce or row_union)."
          },
          "description": {
            "type": [
              "string",
              "null"
            ],
            "description": "One short sentence of plain prose saying what this step does, shown to reviewers on the Spec tab. Supply it when creating the step and refresh it whenever you change what the step does. Informational only — never affects validation or execution."
          },
          "scope_name": {
            "type": [
              "string",
              "null"
            ],
            "description": "Scope identifier for the EXPAND group this collector closes (collector only)."
          },
          "scope_opener": {
            "type": [
              "string",
              "null"
            ],
            "description": "Node id of the multi-row transform that opens the collector's EXPAND group (collector only)."
          },
          "scope_policy": {
            "type": [
              "string",
              "null"
            ],
            "description": "Group arrival policy (collector only): 'require_all' or 'best_effort'. REQUIRED for collectors — there is no default; the author decides whether a lost member fails the group."
          }
        },
        "required": [
          "id",
          "node_type",
          "input"
        ],
        "additionalProperties": false
      },
      "description": "Node specs. A queue node is a structural fan-in point: node_type='queue', id == input == the shared connection name, plugin and every routing field omitted, options at most an optional description; multiple producers may publish that name precisely because the queue is declared. A row_union is plugin-free require_all fork reconvergence: at least two ordered branches, input set to the first branch connection, on_success published to downstream processing (not a sink), no options/routing/policy/merge fields, optional timeout_seconds. A collector closes a declared EXPAND scope: node_type='collector', a batch-aware plugin, and the scope binding authored on the node — scope_name, scope_opener (the multi-row transform that opens the group), and scope_policy ('require_all' or 'best_effort', no default)."
    },
    "edges": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "id": {
            "type": "string"
          },
          "from_node": {
            "type": "string"
          },
          "to_node": {
            "type": "string"
          },
          "edge_type": {
            "type": "string",
            "enum": [
              "on_success",
              "on_error",
              "route_true",
              "route_false",
              "fork"
            ]
          },
          "label": {
            "type": [
              "string",
              "null"
            ]
          }
        },
        "required": [
          "id",
          "from_node",
          "to_node",
          "edge_type"
        ],
        "additionalProperties": false
      },
      "description": "Edge specs. edge_type='on_error' is supported for transform/aggregation sink wiring only; a gate's evaluation-error policy is the node's on_error field. Advisory display records: node connection fields are the authoritative runtime wiring, and [] is complete for a fully-wired topology."
    },
    "outputs": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "sink_name": {
            "type": "string",
            "description": "Sink identifier and routing target: name it from an upstream success/gate route or an eligible source-validation, processing-error or sink-write-failure policy. It need not match a node id."
          },
          "plugin": {
            "type": "string"
          },
          "options": {
            "type": "object",
            "description": "Plugin-specific sink config. The interpretation_requirements option root is not settable on outputs. Stage an authorable review on its source or node instead."
          },
          "on_write_failure": {
            "type": "string",
            "description": "Required explicit non-empty failure choice; full replacement does not reuse prior choices."
          },
          "description": {
            "type": [
              "string",
              "null"
            ],
            "description": "One short sentence of plain prose saying what this step does, shown to reviewers on the Spec tab. Supply it when creating the step and refresh it whenever you change what the step does. Informational only — never affects validation or execution."
          }
        },
        "required": [
          "sink_name",
          "plugin",
          "on_write_failure"
        ],
        "additionalProperties": false,
        "examples": [
          {
            "sink_name": "results",
            "plugin": "json",
            "options": {
              "path": "outputs/results.json",
              "schema": {
                "mode": "observed"
              },
              "mode": "write",
              "collision_policy": "auto_increment"
            },
            "on_write_failure": "discard"
          }
        ]
      },
      "description": "Output specs. For csv/document/json/text file sinks in runnable web pipelines, options must include path, schema, explicit mode ('write' or 'append'), and explicit collision_policy. Sink schema.fields and custom header keys use carried row names, normally normalized source headers or mapping targets."
    },
    "metadata": {
      "type": [
        "object",
        "null"
      ],
      "additionalProperties": false,
      "description": "Pipeline metadata: {name?, description?}",
      "properties": {
        "name": {
          "type": [
            "string",
            "null"
          ]
        },
        "description": {
          "type": [
            "string",
            "null"
          ]
        }
      }
    }
  },
  "oneOf": [
    {
      "required": [
        "source"
      ]
    },
    {
      "required": [
        "sources"
      ]
    }
  ],
  "required": [
    "nodes",
    "edges",
    "outputs"
  ],
  "additionalProperties": false
}
```
