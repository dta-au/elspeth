"""All parameterless tool contracts through OpenRouter; wire canary, not execution."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from scripts.composer_acceptance.budget import ProviderBudget, observe_provider_requests
from scripts.composer_acceptance.serve import fingerprint, load_authorized_key


async def run(output: Path, model: str) -> dict[str, Any]:
    import litellm

    from elspeth.contracts.composer_llm_audit import ToolContractDialect
    from elspeth.web.composer.service import composer_loop_tool_definitions
    from elspeth.web.composer.tools._dispatch import get_tool_definitions
    from elspeth.web.composer.tools.wire_projection import (
        decode_wire_arguments,
        encode_semantic_arguments,
    )

    dialect = ToolContractDialect.OPENAI_STRICT
    definitions = {tool["name"]: tool for tool in get_tool_definitions()}
    names = [name for name, tool in definitions.items() if not tool["parameters"].get("properties")]
    tools = composer_loop_tool_definitions(dialect)
    results: dict[str, Any] = {"scope": "wire_only_no_tool_dispatch", "declared_tool_count": len(tools), "calls": []}
    for name in names:
        encoded = encode_semantic_arguments(name, dialect, {})
        decoded = decode_wire_arguments(name, dialect, encoded)
        if not decoded.wire_conformant or dict(decoded.semantic) != {}:
            raise ValueError("Production parameterless codec roundtrip failed")
        row: dict[str, Any] = {"tool": name}
        try:
            response = await litellm.acompletion(
                model=model,
                messages=[{"role": "user", "content": f"Call {name} exactly once. Do not call any other tool."}],
                tools=tools,
                tool_choice="auto",
                extra_body={"provider": {"order": ["Together"], "allow_fallbacks": False}},
                max_tokens=2048,
                timeout=180,
                num_retries=0,
            )
            calls = response.choices[0].message.tool_calls or []
            valid = len(calls) == 1 and calls[0].function.name == name
            if valid:
                raw = json.loads(calls[0].function.arguments)
                parsed = decode_wire_arguments(name, dialect, raw)
                valid = (
                    parsed.wire_conformant
                    and dict(parsed.semantic) == {}
                    and Draft202012Validator(definitions[name]["parameters"]).is_valid(dict(parsed.semantic))
                )
            row.update(passed=valid, tool_call_count=len(calls), request_id=response.id)
        except Exception as exc:
            row.update(passed=False, error_class=type(exc).__name__)
        results["calls"].append(row)
        (output / "canary.json").write_text(json.dumps(results, indent=2) + "\n")
        print(json.dumps(row), flush=True)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default="openrouter/deepseek/deepseek-v4.1-flash")
    args = parser.parse_args()
    if not args.execute:
        print("No calls made. --execute runs one AUTO Together-pinned call per production parameterless tool.")
        return
    if args.env_file is None:
        parser.error("--execute requires --env-file")
    args.output.mkdir(parents=True, exist_ok=True)
    load_authorized_key(args.env_file)
    root = Path(__file__).resolve().parents[2]
    before = fingerprint(root)
    budget = ProviderBudget(args.output / "provider-ledger.jsonl")
    with observe_provider_requests(budget):
        results = asyncio.run(run(args.output, args.model))
    after = fingerprint(root)
    results.update(frozen=before["content_sha256"] == after["content_sha256"], source_before=before, source_after=after)
    (args.output / "canary.json").write_text(json.dumps(results, indent=2) + "\n")
    if not results["frozen"] or not all(row["passed"] for row in results["calls"]):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
