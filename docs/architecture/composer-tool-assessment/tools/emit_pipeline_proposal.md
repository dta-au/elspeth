# `emit_pipeline_proposal` — planner-only complete proposal terminal

Baseline: `3b28d537be98234da8135c418bf185a998c18b68`, restored 0.8.9.

## 1. Purpose and usage

This is the planner's terminal call for submitting one complete provider-authored
pipeline after capability discovery. On rejection the model repairs and
resubmits the complete document. It is not an incremental graph mutation, an
execution command, or a success announcement to the user.

Only the planner route exposes it. The ordinary canonical list and freeform
loop do not; the latter uses `set_pipeline`. Composer MCP does not advertise it.
The planner must have an admitted request, immutable capability/policy context,
provider invocation ownership, candidate finalizer/validator and session-backed
custody for the eventual proposal. The tool is available on both dialects but
explicitly non-strict even on `openai_strict` because its full-document schema
is not a strict-capable transport grammar.

## 2. Contract

The root is closed and has one required field, `pipeline`, containing the
canonical `set_pipeline` document schema. The complete schema is appended below.
The inner document is validated again by `SetPipelineArgumentsModel`, so passing
JSON Schema is necessary but not sufficient. The planner additionally rejects
sourceless candidates. Omitted fields, explicit null and authored field presence
must retain their canonical meaning; the server must not infer missing topology
as a substitute for provider authorship.

The terminal must be alone. A terminal with discovery siblings gets rejecting
tool feedback and consumes repair budget without dispatching siblings; multiple
terminal calls are malformed. Successful admission runs the required-control
finalizer, candidate validation and plan construction, yielding a `PipelinePlan`
with provider-authored/effective identity, validation and audit evidence. The
application still stages/publishes the proposal and review; a validated plan is
not proof of session commit, user approval or execution readiness.

Failures include wire/schema/Pydantic rejection, unavailable plugins/profiles,
missing sources, semantic/runtime validation, invalid review references, repeated
rejections, provider failures, response truncation and expired/budget-exhausted
turns. The planner owns retries, deadline, repair/composition/tool-call limits
and optional configured escape hatch. Both LiteLLM retry spellings are pinned
to zero so physical attempts remain individually budgeted/audited. No candidate
cache or general idempotency key is advertised. Repeated-rejection fingerprints
inform repair; they are not an exactly-once commit guarantee. Session operation
fences and proposal authority own concurrent publication.

## 3. Implementation trace

- [Terminal payload/definition](../../../../src/elspeth/web/composer/pipeline_planner.py#L1569),
  [policy-selected planner exposure](../../../../src/elspeth/web/composer/pipeline_planner.py#L1647),
  [provider retry ownership](../../../../src/elspeth/web/composer/pipeline_planner.py#L1605).
- [Provider response parsing](../../../../src/elspeth/web/composer/pipeline_planner.py#L1794),
  [terminal singleton protocol](../../../../src/elspeth/web/composer/pipeline_planner.py#L4401),
  [schema, Pydantic and missing-source gates](../../../../src/elspeth/web/composer/pipeline_planner.py#L4454).
- [Pydantic feedback](../../../../src/elspeth/web/composer/pipeline_planner.py#L2859),
  [required-control finalization and plan construction](../../../../src/elspeth/web/composer/pipeline_planner.py#L4525),
  [plan assembly helper](../../../../src/elspeth/web/composer/pipeline_planner.py#L3041).
- [Planner application](../../../../src/elspeth/web/composer/planning_application.py#L1035)
  bridges the plan into session-backed staging/review;
  [pipeline commit authority](../../../../src/elspeth/web/composer/pipeline_commit.py)
  binds authored/effective content to audit and durable publication.
- [Proposal routes](../../../../src/elspeth/web/sessions/routes/composer/proposals.py)
  handle user acceptance/refusal; [turn persistence](../../../../src/elspeth/web/sessions/composer_turn.py)
  owns terminal response. Browser workspace reads resulting state/proposals;
  it does not consume the private planner terminal as an execution receipt.

## 4. Alternative paths

Discovery can precede one valid proposal; a rejection can lead to corrected
full-document resubmission. Required controls may alter the effective candidate
under explicit server-owned admission policy while the provider's authored
identity is retained. This is an admission safeguard, not permission for the
server to author a replacement pipeline.

Mixed terminal/discovery replies reject the whole batch before dispatch, close
the tool protocol with feedback and spend repair budget. Truncated responses
discard even parseable candidate calls before validation. Exhausted ordinary
repair can enter a configured independent model hatch; a hatch failure preserves
the original exhaustion classification. Recovery controls remain available and
can terminate planning without candidate construction. A strict route still
admits this terminal locally through its non-strict schema and parser.

## 5. Usability assessment

**Intended restriction, high confidence:** full-document repair, planner-only
availability and explicit `strict: false` are deliberate. The model must finish
discovery in earlier turns; freeform callers select `set_pipeline` instead.
Transport strictness is not a substitute for server validation.

**Structural limitation, high confidence in source, moderate in operational
impact:** proposal meaning is distributed across terminal parsing, set-pipeline
models, required controls, plan construction, application staging, review and
commit settlement. A terminal success cannot tell the orchestrator which later
obligation remains unless it interprets these distinct outcomes correctly. No
specific invalid commit or provider-bypass defect is demonstrated here; the
focused protocol tests give counterevidence to those claims.

## 6. Structural remediation

Retain the current planner boundary and provider authorship. Give the proposal
flow a shared owned lifecycle contract: authored candidate → admitted effective
candidate → staged proposal → approved/committed or refused/stale. The planner
and freeform `set_pipeline` should consume the same candidate admission and
receipt vocabulary while preserving their legitimate exposure/wire differences.
Keep execution readiness separate from model completion and proposal acceptance.

Owners are Composer orchestration for budgets/terminal decisions, candidate
admission for validation/required controls, plugins for configuration semantics,
and Sessions for atomic publication and evidence. The related tools are
`set_pipeline`, incremental graph edits, `preview_pipeline`, advisor/interpretation
review and recovery terminals. Strengthen current seams rather than replacing
them with an unbounded agent framework.

Keep existing tool names and omission/null semantics. Any stored proposal,
review or receipt envelope change needs explicit compatibility handling; restored
0.8.9 state is not disposable. Acceptance must prove valid end-to-end staging,
schema and semantic rejection without publication, no sibling effects on mixed
terminal calls, truncation rejection before finalization, unchanged deadline
through repair, one audit per physical provider attempt, stale approval refusal,
and required-control/provider-authority provenance on both entry paths.

## 7. Evidence, confidence, risk and limits

Read and executed meaningful tests in
[planner protocol rejections](../../../../tests/unit/web/composer/test_pipeline_planner_protocol_rejections.py),
[strict dialect](../../../../tests/unit/web/composer/test_planner_strict_dialect.py),
and [planner recovery](../../../../tests/unit/web/composer/test_planner_graph_recovery.py)
within the parent selection: **86 passed, exit 0**. Assertions cover invalid
batches without dispatch, classified budgets, discarded length-stopped calls,
deadline preservation, physical-attempt audits and terminal non-strict stamps.
The review located but did not independently execute all proposal-lifecycle and
runtime acceptance tests. No live provider, PostgreSQL contention or browser
execution was exercised. Source/local protocol confidence is high; a claim of
full end-to-end readiness would be unsupported. Unifying lifecycle receipts has
high integrity/compatibility risk if authored identity, required controls,
fencing or review evidence is weakened; proceed by bounded contract slices.

## Runtime argument schema and measured exposure

This async/terminal control has no synchronous declaration; its specialized owner is traced above.

| Surface | Exposed |
|---|---|
| Canonical emitter | no |
| Freeform loop | no |
| Planner default | yes |
| Composer MCP | no |

Both measured provider dialects have the same name membership. Wire schemas, required-null projection, phase/policy filtering and available dependencies can still differ; exposure does not certify execution readiness.

This is the runtime-emitted canonical input schema, before provider wire projection. Public dispatch closes the root object where necessary; owned argument models and kind-specific/domain guards can impose additional constraints described above. Dynamic plugin options are governed by the selected plugin schema and policy projection, not by arbitrary object permissiveness here.

```json
{
  "type": "object",
  "properties": {
    "pipeline": {
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
              "description": "Connection-name string the source PUBLISHES. Some downstream consumer (node 'input' or output 'sink_name') MUST equal this. Connections match by string, not by node id.",
              "pattern": "^[a-zA-Z0-9_][a-zA-Z0-9_-]*$",
              "maxLength": 64,
              "not": {
                "anyOf": [
                  {
                    "enum": [
                      "continue",
                      "fork",
                      "on_success"
                    ]
                  },
                  {
                    "type": "string",
                    "pattern": "^__"
                  }
                ]
              }
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
                "type": "string",
                "pattern": "^[a-zA-Z0-9_][a-zA-Z0-9_-]*$",
                "maxLength": 64,
                "not": {
                  "anyOf": [
                    {
                      "enum": [
                        "continue",
                        "fork",
                        "on_success"
                      ]
                    },
                    {
                      "type": "string",
                      "pattern": "^__"
                    }
                  ]
                }
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
          },
          "propertyNames": {
            "pattern": "^[a-z][a-z0-9_-]*$",
            "maxLength": 38,
            "not": {
              "enum": [
                "continue",
                "fork",
                "on_success"
              ]
            }
          }
        },
        "nodes": {
          "type": "array",
          "items": {
            "type": "object",
            "properties": {
              "id": {
                "type": "string",
                "pattern": "^[a-zA-Z][a-zA-Z0-9_-]*$",
                "maxLength": 38,
                "not": {
                  "enum": [
                    "continue",
                    "fork",
                    "on_success"
                  ]
                }
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
                "description": "Upstream published connection this node consumes, normally an on_success or gate route. Valid implicit aggregation/queue/coalesce publications can equal the publisher's node id. For coalesce/row_union use their branch/input rules. Error routes cannot feed arbitrary processing nodes; only a permitted enclosing bound-region closer is eligible.",
                "pattern": "^[a-zA-Z0-9_][a-zA-Z0-9_-]*$",
                "maxLength": 64,
                "not": {
                  "anyOf": [
                    {
                      "enum": [
                        "continue",
                        "fork",
                        "on_success"
                      ]
                    },
                    {
                      "type": "string",
                      "pattern": "^__"
                    }
                  ]
                }
              },
              "on_success": {
                "type": [
                  "string",
                  "null"
                ],
                "description": "Connection-name string this node PUBLISHES; some downstream input/sink_name MUST equal it. Omit for gates (they route via condition+routes). A coalesce publishes under its own node id, and its on_success, when set, may ONLY name a sink. A row_union's on_success must name a downstream processing connection, never a sink.",
                "pattern": "^[a-zA-Z0-9_][a-zA-Z0-9_-]*$",
                "maxLength": 64,
                "not": {
                  "anyOf": [
                    {
                      "enum": [
                        "continue",
                        "fork",
                        "on_success"
                      ]
                    },
                    {
                      "type": "string",
                      "pattern": "^__"
                    }
                  ]
                }
              },
              "on_error": {
                "type": [
                  "string",
                  "null"
                ],
                "minLength": 1,
                "description": "Node-level error policy (transform/aggregation/gate): 'discard' or a declared sink name. A transform or gate inside a valid bound region may instead divert loss to that enclosing region's closer; aggregations cannot. Arbitrary processing inputs are forbidden. Transforms/aggregations require an explicit non-empty choice; full replacement does not reuse prior choices. For a gate it covers row expression-evaluation errors and is authored on the node, never as an edge; omit it to preserve fail-fast behavior. A failed aggregation batch applies this policy to every buffered row; discard records quarantine outcomes.",
                "pattern": "^[a-z0-9_][a-z0-9_-]*$",
                "maxLength": 38,
                "not": {
                  "anyOf": [
                    {
                      "enum": [
                        "continue",
                        "fork",
                        "on_success"
                      ]
                    },
                    {
                      "type": "string",
                      "pattern": "^__"
                    }
                  ]
                }
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
                  "type": "string",
                  "pattern": "^[a-zA-Z0-9_][a-zA-Z0-9_-]*$",
                  "maxLength": 64,
                  "not": {
                    "anyOf": [
                      {
                        "enum": [
                          "continue",
                          "on_success"
                        ]
                      },
                      {
                        "type": "string",
                        "pattern": "^__"
                      }
                    ]
                  }
                },
                "description": "Gate route mapping to sink names, downstream connection names, 'fork', or 'discard' for an audited terminal drop.",
                "propertyNames": {
                  "pattern": "^[a-zA-Z0-9_][a-zA-Z0-9_-]*$",
                  "maxLength": 64,
                  "not": {
                    "anyOf": [
                      {
                        "enum": [
                          "continue",
                          "fork",
                          "on_success"
                        ]
                      },
                      {
                        "type": "string",
                        "pattern": "^__"
                      }
                    ]
                  }
                }
              },
              "fork_to": {
                "type": [
                  "array",
                  "null"
                ],
                "items": {
                  "type": "string",
                  "pattern": "^[a-zA-Z0-9_][a-zA-Z0-9_-]*$",
                  "maxLength": 64,
                  "not": {
                    "anyOf": [
                      {
                        "enum": [
                          "continue",
                          "fork",
                          "on_success"
                        ]
                      },
                      {
                        "type": "string",
                        "pattern": "^__"
                      }
                    ]
                  }
                }
              },
              "branches": {
                "type": [
                  "array",
                  "object",
                  "null"
                ],
                "items": {
                  "type": "string",
                  "pattern": "^[a-zA-Z0-9_][a-zA-Z0-9_-]*$",
                  "maxLength": 64,
                  "not": {
                    "anyOf": [
                      {
                        "enum": [
                          "continue",
                          "fork",
                          "on_success"
                        ]
                      },
                      {
                        "type": "string",
                        "pattern": "^__"
                      }
                    ]
                  }
                },
                "additionalProperties": {
                  "type": "string",
                  "pattern": "^[a-zA-Z0-9_][a-zA-Z0-9_-]*$",
                  "maxLength": 64,
                  "not": {
                    "anyOf": [
                      {
                        "enum": [
                          "continue",
                          "fork",
                          "on_success"
                        ]
                      },
                      {
                        "type": "string",
                        "pattern": "^__"
                      }
                    ]
                  }
                },
                "description": "Branch inputs for coalesce or row_union. For row_union, every branches value is a real consumed input connection; input must repeat the first value only as an adapter placeholder.",
                "propertyNames": {
                  "pattern": "^[a-zA-Z0-9_][a-zA-Z0-9_-]*$",
                  "maxLength": 64,
                  "not": {
                    "anyOf": [
                      {
                        "enum": [
                          "continue",
                          "fork",
                          "on_success"
                        ]
                      },
                      {
                        "type": "string",
                        "pattern": "^__"
                      }
                    ]
                  }
                }
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
                "description": "Sink identifier and routing target: name it from an upstream success/gate route or an eligible source-validation, processing-error or sink-write-failure policy. It need not match a node id.",
                "pattern": "^[a-z0-9_][a-z0-9_-]*$",
                "maxLength": 38,
                "not": {
                  "anyOf": [
                    {
                      "enum": [
                        "continue",
                        "fork",
                        "on_success"
                      ]
                    },
                    {
                      "type": "string",
                      "pattern": "^__"
                    }
                  ]
                }
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
                "description": "Required explicit non-empty failure choice; full replacement does not reuse prior choices.",
                "pattern": "^[a-z0-9_][a-z0-9_-]*$",
                "maxLength": 38,
                "not": {
                  "anyOf": [
                    {
                      "enum": [
                        "continue",
                        "fork",
                        "on_success"
                      ]
                    },
                    {
                      "type": "string",
                      "pattern": "^__"
                    }
                  ]
                }
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
  },
  "required": [
    "pipeline"
  ],
  "additionalProperties": false
}
```
