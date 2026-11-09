"""Run real Chroma tests with its verified model cache in a writable lane."""
from pathlib import Path
import hashlib
import sys

import pytest

CACHE = Path(__file__).parent / "runtime-deps/chroma/all-MiniLM-L6-v2"


class LocalModelCache:
    def pytest_configure(self, config):
        from chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 import ONNXMiniLM_L6_V2

        with (CACHE / "onnx.tar.gz").open("rb") as asset:
            assert hashlib.file_digest(asset, "sha256").hexdigest() == ONNXMiniLM_L6_V2._MODEL_SHA256
        ONNXMiniLM_L6_V2.DOWNLOAD_PATH = CACHE
        print(f"Real Chroma ONNX cache: {CACHE}", flush=True)


if __name__ == "__main__":
    raise SystemExit(pytest.main(sys.argv[1:], plugins=[LocalModelCache()]))
