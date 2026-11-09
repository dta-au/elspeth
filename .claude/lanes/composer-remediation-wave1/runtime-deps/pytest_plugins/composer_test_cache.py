"""Local test runtime setup; never replaces Chroma's real embedding function."""
from pathlib import Path
import hashlib


def pytest_configure(config):
    # Defer imports until the project's initial conftest configured its runtime.
    import elspeth
    import elspeth_lints
    from chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 import ONNXMiniLM_L6_V2

    root = Path(config.rootpath).resolve()
    assert Path(elspeth.__file__).is_relative_to(root / "src")
    assert Path(elspeth_lints.__file__).is_relative_to(root / "elspeth-lints/src")
    cache = Path(__file__).resolve().parents[1] / "chroma/all-MiniLM-L6-v2"
    expected = "913d7300ceae3b2dbc2c50d1de4baacab4be7b9380491c27fab7418616a16ec3"
    assert ONNXMiniLM_L6_V2._MODEL_SHA256 == expected
    with (cache / "onnx.tar.gz").open("rb") as archive:
        assert hashlib.file_digest(archive, "sha256").hexdigest() == expected
    ONNXMiniLM_L6_V2.DOWNLOAD_PATH = cache
    print(f"Real Chroma model cache configured: {cache}", flush=True)
