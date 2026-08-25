"""Repro: step-1 guided chat with the collector calibration prompt.

Tees the raw provider response out of _bounded_acompletion so the exact
tool-call shape that triggered DeferredIntentActionShapeError is visible.
"""

import asyncio
import json
import sys

sys.path.insert(0, "/home/john/elspeth/src")

import elspeth.web.composer.guided.chat_solver as cs

PROMPT = (
    "Read this synthetic multi-document JSON file, split each document into one row per "
    "section, have an LLM write a one-sentence gist of each section, then gather each "
    "document's section rows back together into a single batch per document (every section "
    "must make it back — fail the document if one is lost) and write one summary row per "
    "document to a JSON file.\n"
    "https://dta-au.github.io/elspeth/tutorial-site/multi-doc-sections.json"
)

captured = []
orig = cs._bounded_acompletion


async def tee(kwargs, timeout_seconds):
    response = await orig(kwargs, timeout_seconds)
    try:
        msg = response.choices[0].message
        calls = [{"name": tc.function.name, "arguments": tc.function.arguments} for tc in (msg.tool_calls or ()) if tc.function is not None]
        captured.append({"content": msg.content, "tool_calls": calls})
    except Exception as exc:  # capture failures must not mask the real path
        captured.append({"capture_error": repr(exc)})
    return response


cs._bounded_acompletion = tee


async def main():
    try:
        outcome = await cs.maybe_resolve_step_1_source_chat(
            model="openrouter/anthropic/claude-sonnet-5",
            user_message=PROMPT,
            plugin_hint=None,
            current_source=None,
            available_source_plugins=("csv", "json", "text", "llm"),
            temperature=None,
            seed=None,
            recorder=None,
            timeout_seconds=120.0,
        )
        print("OUTCOME:", type(outcome).__name__)
    except Exception as exc:
        print("EXC:", type(exc).__name__, "|", str(exc)[:500])
    print("=== captured provider replies ===")
    for i, c in enumerate(captured):
        print(f"--- reply {i} ---")
        print(json.dumps(c, indent=1)[:6000])


asyncio.run(main())
