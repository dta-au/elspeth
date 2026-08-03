"""Keep the operator token-outcome SQL aligned with the closed runtime contract."""

from pathlib import Path

from elspeth.contracts.audit import _TERMINAL_PAIR_FIELD_CONSTRAINTS
from elspeth.contracts.enums import _LEGAL_TERMINAL_PAIRS

_ROOT = Path(__file__).parents[3]


def test_token_outcome_contract_table_lists_every_legal_terminal_pair() -> None:
    contract = (_ROOT / "docs/contracts/token-outcomes/00-token-outcome-contract.md").read_text()

    for outcome, path in _LEGAL_TERMINAL_PAIRS:
        assert f"| 1 | `{outcome.value}` | `{path.value}` |" in contract


def test_audit_sweep_sql_admits_every_legal_pair_and_checks_required_error_hashes() -> None:
    sweep = (_ROOT / "docs/contracts/token-outcomes/02-audit-sweep.md").read_text()
    required_section = sweep.split("## 5. Required Discriminator Fields Missing", 1)[1].split("## 6.", 1)[0]

    for outcome, path in _LEGAL_TERMINAL_PAIRS:
        assert f"('{outcome.value}', '{path.value}')" in sweep
        constraints = _TERMINAL_PAIR_FIELD_CONSTRAINTS[(outcome, path)]
        if "error_hash" in constraints.required:
            assert f"'{path.value}'" in required_section
