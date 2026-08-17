"""corpus.md parsing — the first unlabelled fenced block under each ``## <case>`` heading is the
prompt, copied byte-for-byte to the wire (ops-local/acceptance/extract_intents.py discipline)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS_PATH = REPO_ROOT / "evals/composer-battery/corpus.md"
SCENARIOS_DIR = REPO_ROOT / "evals/composer-battery/scenarios"

_HEADING = re.compile(r"^## (?P<name>[a-z0-9_]+)\s*$", re.MULTILINE)
_VERSION = re.compile(r"^corpus_version:\s*(?P<v>\d+)\s*$", re.MULTILINE)
_FENCE = re.compile(r"^```(?P<lang>[^\n]*)\n(?P<body>.*?)^```\s*$", re.MULTILINE | re.DOTALL)


@dataclass(frozen=True)
class CorpusCase:
    name: str
    prompt: str


def parse_corpus(md: str) -> tuple[int, list[CorpusCase]]:
    vm = _VERSION.search(md)
    if vm is None:
        raise ValueError("corpus.md has no `corpus_version: N` line")
    version = int(vm.group("v"))
    headings = list(_HEADING.finditer(md))
    cases: list[CorpusCase] = []
    for i, h in enumerate(headings):
        start = h.end()
        end = headings[i + 1].start() if i + 1 < len(headings) else len(md)
        section = md[start:end]
        prompt: str | None = None
        for fm in _FENCE.finditer(section):
            if fm.group("lang").strip() == "":
                body = fm.group("body")
                prompt = body[:-1] if body.endswith("\n") else body
                break
        if prompt is None:
            raise ValueError(f"case {h.group('name')!r} has no unlabelled fenced prompt")
        cases.append(CorpusCase(h.group("name"), prompt))
    duplicates = sorted({c.name for c in cases if sum(1 for other in cases if other.name == c.name) > 1})
    if duplicates:
        # load_corpus keys by name, so a duplicated heading would silently keep the LAST prompt while the
        # scenario, the floor and every captured prompt_sha256 still refer to the first — refuse instead.
        raise ValueError(f"corpus.md declares duplicate `## case` headings: {duplicates}")
    return version, cases


def load_corpus(path: Path = CORPUS_PATH) -> tuple[int, dict[str, CorpusCase]]:
    version, cases = parse_corpus(path.read_text())
    return version, {c.name: c for c in cases}


__all__ = ["CORPUS_PATH", "SCENARIOS_DIR", "CorpusCase", "load_corpus", "parse_corpus"]
