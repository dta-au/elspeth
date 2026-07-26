import hashlib
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
BANNED_STAGING_TOKEN_DIGESTS = {
    "a334f89e832bb5ffeb1715f62e03d0b6117fabccab597ddd6a5de201fea39c66",
    "4b66b7c12be17776f3adb435f58d0dffc85775c093f393b6eea3d980a3f3c0c3",
}
RUNNABLE_STAGING_CREDENTIAL_SURFACES = (
    Path("src/elspeth/web/frontend/playwright.staging.config.ts"),
    Path("src/elspeth/web/frontend/tests/e2e/harness/README.md"),
    Path("src/elspeth/web/frontend/tests/e2e/composer-guided-ab-live.staging.spec.ts"),
    Path("src/elspeth/web/frontend/tests/e2e/tutorial-probe.staging.spec.ts"),
)


def _contains_retired_staging_credential(text: str) -> bool:
    return any(hashlib.sha256(token.encode()).hexdigest() in BANNED_STAGING_TOKEN_DIGESTS for token in re.findall(r"[A-Za-z0-9_]+", text))


def test_public_docs_do_not_embed_staging_credential_placeholders() -> None:
    offenders: list[str] = []

    for path in [*REPO_ROOT.glob("*.md"), *REPO_ROOT.glob("docs/**/*.md")]:
        text = path.read_text(encoding="utf-8")
        if _contains_retired_staging_credential(text):
            offenders.append(path.relative_to(REPO_ROOT).as_posix())

    assert offenders == []


def test_runnable_staging_examples_do_not_embed_concrete_credentials() -> None:
    offenders: list[str] = []

    for relative_path in RUNNABLE_STAGING_CREDENTIAL_SURFACES:
        text = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
        if _contains_retired_staging_credential(text):
            offenders.append(relative_path.as_posix())

    assert offenders == []
