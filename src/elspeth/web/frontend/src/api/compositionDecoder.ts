import type { CompositionState } from "@/types/index";

const COMPOSITION_NODE_TYPES = new Set([
  "transform",
  "gate",
  "aggregation",
  "coalesce",
  "row_union",
  "queue",
  "collector",
]);

const COMPOSITION_EDGE_TYPES = new Set(["on_success", "on_error", "route_true", "route_false", "fork"]);

const POLICY_REASONS = new Set([
  "plugin_not_enabled", "plugin_not_installed", "plugin_unavailable",
  "credential_unavailable", "profile_unavailable",
]);

function invalid(path: string, detail: string): never {
  throw new Error(`Invalid composition state at ${path}: ${detail}`);
}

function record(value: unknown, path: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    invalid(path, "expected object");
  }
  return value as Record<string, unknown>;
}

function exactRecord(
  value: unknown,
  path: string,
  required: readonly string[],
  optional: readonly string[] = [],
): Record<string, unknown> {
  const result = record(value, path);
  const allowed = new Set([...required, ...optional]);
  for (const key of required) {
    if (!Object.prototype.hasOwnProperty.call(result, key)) invalid(path, `missing ${key}`);
  }
  for (const key of Object.keys(result)) {
    if (!allowed.has(key)) invalid(path, `unexpected ${key}`);
  }
  return result;
}

function stringValue(value: unknown, path: string): string {
  if (typeof value !== "string") invalid(path, "expected string");
  return value;
}

function nullableString(value: unknown, path: string): string | null {
  return value === null ? null : stringValue(value, path);
}

function booleanValue(value: unknown, path: string): boolean {
  if (typeof value !== "boolean") invalid(path, "expected boolean");
  return value;
}

function integerValue(value: unknown, path: string): number {
  if (typeof value !== "number" || !Number.isInteger(value)) invalid(path, "expected integer");
  return value;
}

function arrayValue(value: unknown, path: string): unknown[] {
  if (!Array.isArray(value)) invalid(path, "expected array");
  return value;
}

function stringArray(value: unknown, path: string): string[] {
  return arrayValue(value, path).map((item, index) => stringValue(item, `${path}[${index}]`));
}

function jsonValue(value: unknown, path: string): unknown {
  if (value === null || typeof value === "string" || typeof value === "boolean") return value;
  if (typeof value === "number") {
    if (!Number.isFinite(value)) invalid(path, "expected finite JSON number");
    return value;
  }
  if (Array.isArray(value)) return value.map((item, index) => jsonValue(item, `${path}[${index}]`));
  const decoded = record(value, path);
  return Object.fromEntries(
    Object.entries(decoded).map(([key, item]) => [key, jsonValue(item, `${path}.${key}`)]),
  );
}

function jsonRecord(value: unknown, path: string): Record<string, unknown> {
  const decoded = jsonValue(value, path);
  if (typeof decoded !== "object" || decoded === null || Array.isArray(decoded)) {
    invalid(path, "expected JSON object");
  }
  return decoded as Record<string, unknown>;
}

function finitePositiveNumber(value: unknown, path: string): number {
  if (typeof value !== "number" || !Number.isFinite(value) || value <= 0) {
    invalid(path, "expected finite positive number");
  }
  return value;
}

export function decodeCompositionState(value: unknown, path = "composition_state"): CompositionState {
  const state = exactRecord(
    value,
    path,
    [
      "id", "session_id", "version", "sources", "nodes", "edges", "outputs", "metadata",
      "is_valid", "validation_errors", "validation_warnings",
      "validation_suggestions", "derived_from_state_id", "created_at", "composer_meta",
      "plugin_policy_findings",
    ],
  );
  const id = stringValue(state.id, `${path}.id`);
  const sessionId = stringValue(state.session_id, `${path}.session_id`);
  const version = integerValue(state.version, `${path}.version`);
  const isValid = booleanValue(state.is_valid, `${path}.is_valid`);
  const derivedFromStateId = nullableString(state.derived_from_state_id, `${path}.derived_from_state_id`);
  const createdAt = stringValue(state.created_at, `${path}.created_at`);
  const composerMeta = state.composer_meta === null
    ? null
    : jsonRecord(state.composer_meta, `${path}.composer_meta`);

  const sources: CompositionState["sources"] = {};
  const wireSources = state.sources === null ? {} : record(state.sources, `${path}.sources`);
  for (const [name, item] of Object.entries(wireSources)) {
    const sourcePath = `${path}.sources.${name}`;
    const source = exactRecord(
      item,
      sourcePath,
      ["plugin", "options", "on_success", "on_validation_failure"],
      ["description"],
    );
    sources[name] = {
      plugin: stringValue(source.plugin, `${sourcePath}.plugin`),
      options: jsonRecord(source.options, `${sourcePath}.options`),
      on_success: stringValue(source.on_success, `${sourcePath}.on_success`),
      on_validation_failure: stringValue(source.on_validation_failure, `${sourcePath}.on_validation_failure`),
    };
    if (source.description !== undefined) {
      sources[name].description = nullableString(source.description, `${sourcePath}.description`);
    }
  }

  const wireNodes = state.nodes === null ? [] : arrayValue(state.nodes, `${path}.nodes`);
  const nodes: CompositionState["nodes"] = wireNodes.map((item, index) => {
    const nodePath = `${path}.nodes[${index}]`;
    const node = exactRecord(
      item,
      nodePath,
      ["id", "node_type", "plugin", "input", "on_success", "on_error", "options"],
      [
        "condition",
        "routes",
        "fork_to",
        "branches",
        "policy",
        "merge",
        "trigger",
        "output_mode",
        "expected_output_count",
        "timeout_seconds",
        "description",
        "scope_name",
        "scope_opener",
        "scope_policy",
      ],
    );
    const nodeType = stringValue(node.node_type, `${nodePath}.node_type`);
    if (!COMPOSITION_NODE_TYPES.has(nodeType)) invalid(`${nodePath}.node_type`, "unknown node type");
    const decoded: CompositionState["nodes"][number] = {
      id: stringValue(node.id, `${nodePath}.id`),
      node_type: nodeType as CompositionState["nodes"][number]["node_type"],
      plugin: nullableString(node.plugin, `${nodePath}.plugin`),
      input: stringValue(node.input, `${nodePath}.input`),
      on_success: nullableString(node.on_success, `${nodePath}.on_success`),
      on_error: nullableString(node.on_error, `${nodePath}.on_error`),
      options: jsonRecord(node.options, `${nodePath}.options`),
    };
    if (node.condition !== undefined) decoded.condition = nullableString(node.condition, `${nodePath}.condition`);
    if (node.routes !== undefined) {
      decoded.routes = node.routes === null
        ? null
        : Object.fromEntries(
          Object.entries(record(node.routes, `${nodePath}.routes`)).map(([key, target]) => [key, stringValue(target, `${nodePath}.routes.${key}`)]),
        );
    }
    if (node.fork_to !== undefined) decoded.fork_to = node.fork_to === null ? null : stringArray(node.fork_to, `${nodePath}.fork_to`);
    if (node.branches !== undefined) {
      decoded.branches = node.branches === null
        ? null
        : Array.isArray(node.branches)
          ? stringArray(node.branches, `${nodePath}.branches`)
          : Object.fromEntries(
            Object.entries(record(node.branches, `${nodePath}.branches`)).map(([key, target]) => [key, stringValue(target, `${nodePath}.branches.${key}`)]),
          );
    }
    if (node.policy !== undefined) decoded.policy = nullableString(node.policy, `${nodePath}.policy`);
    if (node.merge !== undefined) decoded.merge = nullableString(node.merge, `${nodePath}.merge`);
    if (node.trigger !== undefined) decoded.trigger = node.trigger === null ? null : jsonRecord(node.trigger, `${nodePath}.trigger`);
    if (node.output_mode !== undefined) {
      const outputMode = nullableString(node.output_mode, `${nodePath}.output_mode`);
      if (outputMode !== null && !["default", "passthrough", "transform"].includes(outputMode)) {
        invalid(`${nodePath}.output_mode`, "unknown aggregation output mode");
      }
      decoded.output_mode = outputMode as CompositionState["nodes"][number]["output_mode"];
    }
    if (node.expected_output_count !== undefined) {
      decoded.expected_output_count = node.expected_output_count === null
        ? null
        : integerValue(node.expected_output_count, `${nodePath}.expected_output_count`);
    }
    if (node.timeout_seconds !== undefined) {
      decoded.timeout_seconds = node.timeout_seconds === null
        ? null
        : finitePositiveNumber(
          node.timeout_seconds,
          `${nodePath}.timeout_seconds`,
        );
    }
    if (node.description !== undefined) {
      decoded.description = nullableString(node.description, `${nodePath}.description`);
    }
    if (node.scope_name !== undefined) {
      decoded.scope_name = nullableString(node.scope_name, `${nodePath}.scope_name`);
    }
    if (node.scope_opener !== undefined) {
      decoded.scope_opener = nullableString(node.scope_opener, `${nodePath}.scope_opener`);
    }
    if (node.scope_policy !== undefined) {
      decoded.scope_policy = nullableString(node.scope_policy, `${nodePath}.scope_policy`);
    }
    return decoded;
  });

  const wireEdges = state.edges === null ? [] : arrayValue(state.edges, `${path}.edges`);
  const edges: CompositionState["edges"] = wireEdges.map((item, index) => {
    const edgePath = `${path}.edges[${index}]`;
    const edge = exactRecord(item, edgePath, ["id", "from_node", "to_node", "edge_type", "label"]);
    const edgeType = stringValue(edge.edge_type, `${edgePath}.edge_type`);
    if (!COMPOSITION_EDGE_TYPES.has(edgeType)) invalid(`${edgePath}.edge_type`, "unknown edge type");
    return {
      id: stringValue(edge.id, `${edgePath}.id`),
      from_node: stringValue(edge.from_node, `${edgePath}.from_node`),
      to_node: stringValue(edge.to_node, `${edgePath}.to_node`),
      edge_type: edgeType as CompositionState["edges"][number]["edge_type"],
      label: nullableString(edge.label, `${edgePath}.label`),
    };
  });

  const wireOutputs = state.outputs === null ? [] : arrayValue(state.outputs, `${path}.outputs`);
  const outputs: CompositionState["outputs"] = wireOutputs.map((item, index) => {
    const outputPath = `${path}.outputs[${index}]`;
    const output = exactRecord(
      item,
      outputPath,
      ["name", "plugin", "options", "on_write_failure"],
      ["description"],
    );
    const decodedOutput: CompositionState["outputs"][number] = {
      name: stringValue(output.name, `${outputPath}.name`),
      plugin: stringValue(output.plugin, `${outputPath}.plugin`),
      options: jsonRecord(output.options, `${outputPath}.options`),
      on_write_failure: stringValue(output.on_write_failure, `${outputPath}.on_write_failure`),
    };
    if (output.description !== undefined) {
      decodedOutput.description = nullableString(output.description, `${outputPath}.description`);
    }
    return decodedOutput;
  });

  const metadata = state.metadata === null
    ? { name: null, description: null }
    : (() => {
      const metadataValue = exactRecord(state.metadata, `${path}.metadata`, ["name", "description"]);
      return {
        name: nullableString(metadataValue.name, `${path}.metadata.name`),
        description: nullableString(metadataValue.description, `${path}.metadata.description`),
      };
    })();
  const decodeValidationEntries = (field: "validation_warnings" | "validation_suggestions") => {
    if (state[field] === null) return null;
    return arrayValue(state[field], `${path}.${field}`).map((item, index) => {
      const itemPath = `${path}.${field}[${index}]`;
      const entry = exactRecord(item, itemPath, ["component", "message", "severity"], ["error_code"]);
      return {
        component: stringValue(entry.component, `${itemPath}.component`),
        message: stringValue(entry.message, `${itemPath}.message`),
        severity: stringValue(entry.severity, `${itemPath}.severity`),
        ...(entry.error_code === undefined
          ? {}
          : { error_code: nullableString(entry.error_code, `${itemPath}.error_code`) }),
      };
    });
  };
  const validationErrors = state.validation_errors === null
    ? null
    : arrayValue(state.validation_errors, `${path}.validation_errors`).map((item, index) => {
      const itemPath = `${path}.validation_errors[${index}]`;
      const error = exactRecord(item, itemPath, ["message", "error_code", "component"]);
      return {
        message: stringValue(error.message, `${itemPath}.message`),
        error_code: nullableString(error.error_code, `${itemPath}.error_code`),
        component: nullableString(error.component, `${itemPath}.component`),
      };
    });
  const validationWarnings = decodeValidationEntries("validation_warnings");
  const validationSuggestions = decodeValidationEntries("validation_suggestions");
  const policyFindings = arrayValue(state.plugin_policy_findings, `${path}.plugin_policy_findings`).map((item, index) => {
      const itemPath = `${path}.plugin_policy_findings[${index}]`;
      const finding = exactRecord(item, itemPath, ["component_id", "plugin_id", "reason_code", "snapshot_fingerprint"]);
      const reasonCode = stringValue(finding.reason_code, `${itemPath}.reason_code`);
      if (!POLICY_REASONS.has(reasonCode)) invalid(`${itemPath}.reason_code`, "unknown policy reason");
      return {
        component_id: stringValue(finding.component_id, `${itemPath}.component_id`),
        plugin_id: stringValue(finding.plugin_id, `${itemPath}.plugin_id`),
        reason_code: reasonCode as NonNullable<CompositionState["plugin_policy_findings"]>[number]["reason_code"],
        snapshot_fingerprint: stringValue(finding.snapshot_fingerprint, `${itemPath}.snapshot_fingerprint`),
      };
    });
  return {
    id,
    session_id: sessionId,
    version,
    sources,
    nodes,
    edges,
    outputs,
    metadata,
    is_valid: isValid,
    validation_errors: validationErrors,
    validation_warnings: validationWarnings,
    validation_suggestions: validationSuggestions,
    derived_from_state_id: derivedFromStateId,
    created_at: createdAt,
    composer_meta: composerMeta,
    plugin_policy_findings: policyFindings,
  };
}

export function decodeCompositionStateVersions(value: unknown): CompositionState[] {
  return arrayValue(value, "composition_states").map((state, index) =>
    decodeCompositionState(state, `composition_states[${index}]`),
  );
}
