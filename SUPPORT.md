# Support

## Current Support Status

ELSPETH is currently a pre-release platform and is not offered with a public
service-level agreement. See the [project overview](README.md) for the current
release status.

## Where To Get Help

- For usage questions, open a GitHub Discussion if discussions are enabled, or a
  GitHub issue labelled as a question.
- For reproducible defects, open a GitHub issue with version, commit, command,
  expected behaviour, actual behaviour, and relevant logs.
- For security-sensitive issues, follow `SECURITY.md` instead of opening a
  public issue with details.
- For evaluation, start with the [project overview](README.md), then use the
  [release documentation index](docs/release/README.md) and
  [Audit and Lineage Guarantees](docs/release/guarantees.md).

## What Maintainers Need

Good support requests include:

- ELSPETH version or commit hash;
- operating system and Python version;
- install method;
- pipeline settings file with secrets removed;
- audit database location if relevant;
- command output or browser console output;
- whether the issue affects audit integrity, secret handling, authentication,
  authorisation, external calls, or release artifacts.

## Boundaries

Maintainers cannot provide:

- agency-specific authority-to-operate approval;
- formal IRAP, PSPF, ISM, Essential Eight, Digital Service Standard, AGDS, or
  WCAG certification;
- guarantees about third-party providers such as Azure OpenAI, OpenRouter,
  Microsoft Entra, ChromaDB, Dataverse, GitHub, or Azure infrastructure;
- support for production use without a separately agreed operational model.
