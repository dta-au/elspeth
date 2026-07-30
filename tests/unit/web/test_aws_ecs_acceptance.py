"""Contract tests for the AWS ECS acceptance controller."""

from __future__ import annotations

import argparse

from elspeth.web import aws_ecs_acceptance as acceptance

EXPECTED_COMMANDS = {
    "capture",
    "provision-storage",
    "scenario-namespace",
    "verify-api",
    "verify-payloads",
    "verify-local-auth",
    "verify-s3",
    "verify-bedrock",
    "verify-bedrock-guardrails",
    "verify-connection-budget",
    "verify-operator-telemetry",
    "extract-exec-receipt",
    "sanitize-evidence",
    "control-manifest",
    "gate-ledger",
    "receipt-store",
    "approval-verify",
    "approval-require-current",
    "scenario-load",
    "validate-task-definition-policy",
    "compatibility-record-validate",
    "orphan-sweep",
    "cleanup-evidence-finalize",
    "evidence-export-receipt",
    "verify-textract",
}


def _all_parsers(parser: argparse.ArgumentParser) -> list[argparse.ArgumentParser]:
    parsers = [parser]
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for child in action.choices.values():
                parsers.extend(_all_parsers(child))
    return parsers


def test_cli_exposes_the_exact_reviewed_command_surface() -> None:
    parser = acceptance.build_parser()
    command_actions = [action for action in parser._actions if isinstance(action, argparse._SubParsersAction)]

    assert len(command_actions) == 1
    assert set(command_actions[0].choices) == EXPECTED_COMMANDS


def test_cli_never_accepts_credentials_or_tokens_as_arguments() -> None:
    parser = acceptance.build_parser()
    option_strings = {option for candidate in _all_parsers(parser) for action in candidate._actions for option in action.option_strings}

    assert not option_strings & {
        "--username",
        "--password",
        "--token",
        "--bearer-token",
        "--access-token",
        "--aws-access-key-id",
        "--aws-secret-access-key",
        "--aws-session-token",
    }


def test_capture_and_verify_api_require_state_file_arguments() -> None:
    parser = acceptance.build_parser()

    capture = parser.parse_args(["capture", "--state-file", "state.json"])
    verify = parser.parse_args(["verify-api", "--state-file", "state.json"])

    assert capture.command == "capture"
    assert capture.state_file == "state.json"
    assert verify.command == "verify-api"
    assert verify.state_file == "state.json"


def test_verify_payloads_requires_landscape_run_id_argument() -> None:
    parser = acceptance.build_parser()

    parsed = parser.parse_args(["verify-payloads", "--landscape-run-id", "6ad6bff9-5e84-48ea-8588-f49cfb93cc62"])

    assert parsed.command == "verify-payloads"
    assert parsed.landscape_run_id == "6ad6bff9-5e84-48ea-8588-f49cfb93cc62"


def test_sanitize_evidence_kinds_are_closed() -> None:
    parser = acceptance.build_parser()
    command_action = next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction))
    sanitizer = command_action.choices["sanitize-evidence"]
    kind_action = next(action for action in sanitizer._actions if action.dest == "kind")

    assert set(kind_action.choices or ()) == {
        "web-log",
        "doctor-log",
        "deployment-event",
        "task-definition",
        "terraform-plan",
        "terraform-destroy-plan",
    }
