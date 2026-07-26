from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
STAGING_CREDENTIAL_PLACEHOLDERS = ("dta_user", "dta_pass")
RUNNABLE_STAGING_CREDENTIAL_SURFACES = (
    Path("src/elspeth/web/frontend/playwright.staging.config.ts"),
    Path("src/elspeth/web/frontend/tests/e2e/harness/README.md"),
    Path("src/elspeth/web/frontend/tests/e2e/composer-guided-ab-live.staging.spec.ts"),
    Path("src/elspeth/web/frontend/tests/e2e/tutorial-probe.staging.spec.ts"),
)


def test_public_docs_do_not_embed_staging_credential_placeholders() -> None:
    offenders: list[str] = []

    for path in [*REPO_ROOT.glob("*.md"), *REPO_ROOT.glob("docs/**/*.md")]:
        text = path.read_text(encoding="utf-8")
        for placeholder in STAGING_CREDENTIAL_PLACEHOLDERS:
            if placeholder in text:
                offenders.append(f"{path.relative_to(REPO_ROOT).as_posix()}: {placeholder}")

    assert offenders == []


def test_runnable_staging_examples_do_not_embed_concrete_credentials() -> None:
    offenders: list[str] = []

    for relative_path in RUNNABLE_STAGING_CREDENTIAL_SURFACES:
        text = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
        for placeholder in STAGING_CREDENTIAL_PLACEHOLDERS:
            if placeholder in text:
                offenders.append(f"{relative_path.as_posix()}: {placeholder}")

    assert offenders == []
