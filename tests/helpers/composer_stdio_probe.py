"""Fresh public SDK client; no application imports or child recovery injection."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

from mcp import ClientSession, StdioServerParameters
from mcp.client import stdio


def linux_owned_children(parent_pid: int) -> list[int]:
    """Measure direct children using PPid; this kernel omits task/children."""
    children = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            status = (entry / "status").read_text()
        except (FileNotFoundError, ProcessLookupError):
            continue
        parent = next(line.split()[1] for line in status.splitlines() if line.startswith("PPid:"))
        if int(parent) == parent_pid:
            children.append(int(entry.name))
    return sorted(children)


async def probe(directory: Path) -> dict:
    directory.mkdir(parents=True, exist_ok=True)
    root = directory / "data"
    root.mkdir(exist_ok=True)
    (root / "input.csv").write_text("text\nhello\n")
    processes = []
    create = stdio._create_platform_compatible_process

    async def observe(*args, **kwargs):
        process = await create(*args, **kwargs)
        processes.append(process)
        return process

    receipt = {}
    parameters = StdioServerParameters(
        command=str(Path(sys.executable).parent / "elspeth-composer"),
        args=["--scratch-dir", str(directory / "scratch"), "--data-dir", str(root)],
        env=dict(os.environ),
    )
    with (directory / "stderr.log").open("w") as errors, patch.object(stdio, "_create_platform_compatible_process", observe):
        async with stdio.stdio_client(parameters, errlog=errors) as streams, ClientSession(*streams) as session:
            await session.initialize()
            if sys.platform == "linux":
                launcher = processes[0].pid
                children = linux_owned_children(launcher)
                assert len(children) == 1, children
                receipt["topology"] = {"supervisor": launcher, "runner": int(children[0])}

            async def call(name, arguments):
                result = await session.call_tool(name, arguments)
                assert len(result.content) == 1
                return json.loads(result.content[0].text) if not result.isError else {"transport_error": result.content[0].text}

            receipt["new"] = await call("new_session", {})
            source = {
                "plugin": "null",
                "on_success": "result",
                "options": {"schema": {"mode": "observed"}},
                "on_validation_failure": "discard",
            }
            output = {
                "sink_name": "result",
                "plugin": "csv",
                "options": {"path": "output.csv", "schema": {"mode": "observed"}, "mode": "write", "collision_policy": "auto_increment"},
                "on_write_failure": "discard",
            }
            receipt["source"] = await call("set_source", source)
            receipt["output"] = await call("set_output", output)
            receipt["preview"] = await call("preview_pipeline", {})
            if "transport_error" not in receipt["preview"]:
                source.update(plugin="csv", options={"path": "input.csv", "schema": {"mode": "observed"}})
                receipt["file_source"] = await call("set_source", source)
                for form in ("source", "sources"):
                    pipeline = {form: source if form == "source" else {"orders": source}, "nodes": [], "edges": [], "outputs": [output]}
                    receipt[form + "_pipeline"] = await call("set_pipeline", pipeline)
                    receipt[form + "_preview"] = await call("preview_pipeline", {})
                    assert receipt[form + "_pipeline"]["success"], receipt[form + "_pipeline"]
                    verdict = receipt[form + "_preview"]
                    assert verdict["data"]["preview_is_valid"], verdict
                    assert {"path_allowlist", "plugin_instantiation", "graph_structure", "schema_compatibility"} <= {
                        check["name"] for check in verdict["runtime_preflight"]["checks"] if check["passed"]
                    }
                receipt["patch"] = await call("patch_output_options", {"sink_name": "result", "patch": {"path": "patched.csv"}})
                assert receipt["patch"]["success"], receipt["patch"]
                sid = receipt["new"]["data"]["session_id"]
                await call("save_session", {"session_id": sid})
                saved_path = directory / "scratch" / f"{sid}.json"
                saved = saved_path.read_bytes()
                outside = directory / "outside.csv"
                outside.write_text("text\nprivate\n")
                (root / "escape.csv").symlink_to(outside)
                for value in (str(outside), "../outside.csv", "escape.csv"):
                    refusal = await call("patch_source_options", {"source_name": "orders", "patch": {"path": value}})
                    assert not refusal["success"] and refusal["validation"]["errors"][0]["error_code"] == "source_path_policy_invalid", (
                        refusal
                    )
                    await call("save_session", {"session_id": sid})
                    assert saved_path.read_bytes() == saved
                receipt["export"] = await call("generate_yaml", {})
                assert receipt["export"]["success"], receipt["export"]
                assert "input.csv" not in json.dumps(receipt["export"])
                assert "patched.csv" not in json.dumps(receipt["export"])
                await call("save_session", {"session_id": sid})
                assert saved_path.read_bytes() == saved
                receipt["private"] = json.loads(saved)
                legacy = json.loads(saved)
                legacy["sources"]["orders"]["options"]["path"] = str(outside)
                legacy["version"] += 1
                saved_path.write_text(json.dumps(legacy))
                escaped = saved_path.read_bytes()
                receipt["legacy_load"] = await call("load_session", {"session_id": sid})
                receipt["legacy_preview"] = await call("preview_pipeline", {})
                assert receipt["legacy_load"]["success"], receipt["legacy_load"]
                verdict = receipt["legacy_preview"]
                assert not verdict["data"]["preview_is_valid"]
                assert any(check["name"] == "path_allowlist" and not check["passed"] for check in verdict["runtime_preflight"]["checks"]), (
                    verdict
                )
                await call("save_session", {"session_id": sid})
                # Loading retains private options; a save canonicalizes JSON.
                assert json.loads(saved_path.read_bytes()) == json.loads(escaped)
                provider_source = {
                    **source,
                    "on_success": "rows",
                    "options": {"path": "PRIVATE-SOURCE.csv", "schema": {"mode": "observed"}},
                }
                provider_output = {**output, "options": {**output["options"], "path": "PRIVATE-OUTPUT.csv"}}
                provider_node = {
                    "id": "retrieval",
                    "node_type": "transform",
                    "plugin": "rag_retrieval",
                    "input": "rows",
                    "on_success": "result",
                    "on_error": "discard",
                    "options": {
                        "provider": "chroma",
                        "provider_config": {"collection": "docs", "persist_directory": "PRIVATE-PROVIDER"},
                        "schema": {"mode": "flexible", "fields": ["text: str"]},
                        "output_prefix": "rag_",
                        "query_field": "text",
                    },
                }
                receipt["provider_author"] = await call(
                    "set_pipeline",
                    {"sources": {"orders": provider_source}, "nodes": [provider_node], "edges": [], "outputs": [provider_output]},
                )
                assert receipt["provider_author"]["success"], receipt["provider_author"]
                # Authoring/export never invokes the provider or a preview.
                receipt["provider_export"] = await call("generate_yaml", {})
                assert receipt["provider_export"]["success"], receipt["provider_export"]
                for private_path in ("PRIVATE-SOURCE.csv", "PRIVATE-OUTPUT.csv", "PRIVATE-PROVIDER"):
                    assert private_path not in json.dumps(receipt["provider_export"])
                await call("save_session", {"session_id": sid})
                provider_saved = saved_path.read_bytes()
                provider_private = json.loads(provider_saved)
                assert provider_private["sources"]["orders"]["options"]["path"] == "PRIVATE-SOURCE.csv"
                assert provider_private["outputs"][0]["options"]["path"] == "outputs/PRIVATE-OUTPUT.csv"
                assert provider_private["nodes"][0]["options"]["provider_config"]["persist_directory"] == "PRIVATE-PROVIDER"
                await call("generate_yaml", {})
                await call("save_session", {"session_id": sid})
                assert saved_path.read_bytes() == provider_saved
                receipt["provider_private"] = provider_private
    receipt["returncode"] = processes[0].returncode
    if sys.platform == "linux":
        assert not Path(f"/proc/{receipt['topology']['supervisor']}").exists()
        assert not Path(f"/proc/{receipt['topology']['runner']}").exists()
    (directory / "receipt.json").write_text(json.dumps(receipt, indent=2))
    assert receipt["source"]["success"] and receipt["output"]["success"], receipt
    assert "transport_error" not in receipt["preview"], receipt["preview"]
    assert receipt["preview"]["data"]["preview_is_valid"], receipt["preview"]
    assert receipt["returncode"] == 0, receipt["returncode"]
    return receipt


if __name__ == "__main__":
    asyncio.run(probe(Path(sys.argv[1])))
