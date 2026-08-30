"""Tracer-bullet: set_source end-to-end through manifest + walker (spec §11).

These tests pin the integration shape established in Task 4 of the Phase 2
redaction plan.  Tasks 13/14/15 replicate the same shape for other tools,
so the assertions here are load-bearing for the bulk-promotion wave.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Annotated, Any

import pytest
from pydantic import BaseModel, ValidationError

from elspeth.contracts.errors import AuditIntegrityError, GuidedCustodyIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.hashing import stable_hash
from elspeth.web.composer.pipeline_proposal import AbsentBase, PipelineProposal, PlannerSurface
from elspeth.web.composer.redaction import (
    MANIFEST,
    REDACTED_BLOB_SOURCE_PATH,
    Sensitive,
    SetSourceArgumentsModel,
    _coerce_stringified_json_object,
    _redact_via_schema,
    _summarize_set_source_options,
    normalize_set_pipeline_redacted_arguments,
    redact_guided_snapshot_storage_paths,
    redact_source_storage_path,
    redact_tool_call_arguments,
)
from elspeth.web.composer.redaction_telemetry import NoopRedactionTelemetry


def _option_shape_summary(
    *,
    mapping: int = 0,
    sequence: int = 0,
    set_: int = 0,
    scalar: int = 0,
) -> dict[str, object]:
    return {
        "_option_shape": "mapping",
        "entry_count": mapping + sequence + set_ + scalar,
        "value_shape_counts": {
            "mapping": mapping,
            "scalar": scalar,
            "sequence": sequence,
            "set": set_,
        },
    }


def test_set_source_manifest_entry_is_type_driven() -> None:
    entry = MANIFEST["set_source"]
    assert entry.argument_model is SetSourceArgumentsModel
    assert entry.policy is None


def test_set_source_argument_model_validates_real_llm_shape() -> None:
    llm_args = {
        "plugin": "csv",
        "options": {"path": "/tmp/data.csv", "header": True},
        "on_success": "rows",
        "on_validation_failure": "discard",
    }
    validated = SetSourceArgumentsModel.model_validate(llm_args)
    assert validated.plugin == "csv"
    assert validated.options == {"path": "/tmp/data.csv", "header": True}
    assert validated.on_success == "rows"
    assert validated.on_validation_failure == "discard"


def test_set_source_argument_model_rejects_missing_required() -> None:
    with pytest.raises(ValidationError):
        SetSourceArgumentsModel.model_validate({})


def test_set_source_argument_model_rejects_wrong_type() -> None:
    with pytest.raises(ValidationError):
        SetSourceArgumentsModel.model_validate(
            {
                "plugin": 42,
                "options": {},
                "on_success": "rows",
                "on_validation_failure": "discard",
            }
        )


def test_set_source_argument_model_rejects_extra_fields() -> None:
    """rev-2 M.1: extra='forbid' prevents argument_canonical/walker drift.

    Without this, a stray ``inline_blob`` or ``label`` field would be
    silently accepted by Pydantic but unrecorded in the walker schema —
    breaking the manifest/canonical-arguments parity invariant the
    adequacy guard relies on.
    """
    with pytest.raises(ValidationError):
        SetSourceArgumentsModel.model_validate(
            {
                "plugin": "csv",
                "options": {"path": "/tmp/x.csv"},
                "on_success": "rows",
                "on_validation_failure": "discard",
                "inline_blob": {"foo": "bar"},  # not a set_source field
            }
        )


def test_redact_substitutes_options_via_summarizer() -> None:
    """Sensitive[options] is replaced by the summarizer string at the top level.

    The summarizer returns canonical JSON of the options shape with scalar
    values redacted.
    Because Sensitive() substitutes the ENTIRE marked value, the top-level
    ``options`` slot in the redacted output is a string (the summarizer
    return), not a dict.  This is the load-bearing shape contract: the
    persistence boundary receives a scalar where a dict would otherwise
    sit.
    """
    tel = NoopRedactionTelemetry()
    args = {
        "plugin": "csv",
        "options": {"path": "/internal/blob/path.csv", "blob_ref": "abc"},
        "on_success": "rows",
        "on_validation_failure": "discard",
    }
    redacted = redact_tool_call_arguments("set_source", args, telemetry=tel)
    assert redacted["plugin"] == "csv"
    assert redacted["on_success"] == "rows"
    assert redacted["on_validation_failure"] == "discard"
    # Sensitive substitution: options is now the summarizer's str output.
    assert isinstance(redacted["options"], str)
    assert json.loads(redacted["options"]) == _option_shape_summary(scalar=2)
    # The original internal path MUST NOT appear in the summary.
    assert "/internal/blob/path.csv" not in redacted["options"]
    # Telemetry recorded the manifest dispatch with the type-driven shape.
    assert tel.manifest_dispatch_calls == [{"tool_name": "set_source", "shape": "type_driven"}]


def test_redact_source_options_summary_hides_paths_without_blob_ref() -> None:
    """Source option summaries must not preserve raw paths without blob_ref."""
    tel = NoopRedactionTelemetry()
    args = {
        "plugin": "csv",
        "options": {"path": "/tmp/data.csv"},
        "on_success": "rows",
        "on_validation_failure": "discard",
    }
    redacted = redact_tool_call_arguments("set_source", args, telemetry=tel)
    assert isinstance(redacted["options"], str)
    assert "/tmp/data.csv" not in redacted["options"]


def test_redact_source_options_summary_hides_credential_values() -> None:
    """Credential-bearing source plugin option values must not survive."""
    tel = NoopRedactionTelemetry()
    raw_connection_string = "DefaultEndpointsProtocol=https;AccountName=acct;AccountKey=KEYVALUE;EndpointSuffix=core.windows.net"
    raw_sas_token = "sig=abcdefghijklmnopqrstuvwxyz1234567890"
    raw_client_secret = "client-secret-value"
    raw_path = "container/private/customer.csv"
    args = {
        "plugin": "azure_blob",
        "options": {
            "connection_string": raw_connection_string,
            "sas_token": raw_sas_token,
            "client_secret": raw_client_secret,
            "container": "private-container",
            "blob_path": raw_path,
        },
        "on_success": "rows",
        "on_validation_failure": "discard",
    }

    redacted = redact_tool_call_arguments("set_source", args, telemetry=tel)
    serialized = json.dumps(redacted, sort_keys=True)

    assert isinstance(redacted["options"], str)
    assert raw_connection_string not in serialized
    assert raw_sas_token not in serialized
    assert raw_client_secret not in serialized
    assert raw_path not in serialized


def test_redact_source_storage_path_masks_file_shape_when_blob_ref_present() -> None:
    """The ``file`` option is an equivalent blob storage-path carrier to ``path``.

    Blob ownership and fork code (blobs/service.py, sessions fork) treat both
    ``path`` and ``file`` as internal storage-path carriers, so a state with
    ``options={"blob_ref": ..., "file": <internal storage_path>}`` must have its
    ``file`` masked too — otherwise the internal blob path leaks through the
    redaction surface (elspeth-a7aa07b7ce).
    """
    state = {
        "source": {
            "plugin": "csv",
            "options": {"file": "/internal/blob/secret-storage.csv", "blob_ref": "abc"},
        }
    }
    redacted = redact_source_storage_path(state)
    assert redacted["source"]["options"]["file"] == REDACTED_BLOB_SOURCE_PATH
    assert "/internal/blob/secret-storage.csv" not in str(redacted)
    # Input is not mutated.
    assert state["source"]["options"]["file"] == "/internal/blob/secret-storage.csv"


def test_redact_source_storage_path_masks_path_shape_when_blob_ref_present() -> None:
    """Regression: the ``path`` shape stays redacted (elspeth-a7aa07b7ce)."""
    state = {"source": {"options": {"path": "/internal/blob/p.csv", "blob_ref": "abc"}}}
    redacted = redact_source_storage_path(state)
    assert redacted["source"]["options"]["path"] == REDACTED_BLOB_SOURCE_PATH


def test_redact_source_storage_path_leaves_manual_file_without_blob_ref() -> None:
    """A manual ``file`` path without ``blob_ref`` is not a blob carrier — unchanged."""
    state = {"source": {"options": {"file": "/tmp/user-data.csv"}}}
    redacted = redact_source_storage_path(state)
    assert redacted["source"]["options"]["file"] == "/tmp/user-data.csv"
    assert REDACTED_BLOB_SOURCE_PATH not in str(redacted)


def test_redact_guided_snapshot_masks_both_channels() -> None:
    """A guided blob source is committed via manual set_source (blob_ref stripped),
    so the committed source carries the real storage_path with NO blob_ref and the
    source-keyed redaction misses it. The co-located schema-8 reviewed source retained
    blob_ref; the helper uses it (no DB lookup) to mask BOTH the committed source
    path (channel 2) and the reviewed snapshot path (channel 3)."""
    real_path = "/home/u/elspeth/data/blobs/sess/abc_data.csv"
    sources = {"source": {"plugin": "csv", "options": {"path": real_path, "schema": {"mode": "observed"}}}}
    composer_meta = {
        "guided_session": {
            "reviewed_sources": {
                "11111111-1111-4111-8111-111111111111": {
                    "name": "source",
                    "plugin": "csv",
                    "options": {
                        "path": real_path,
                        "blob_ref": "11111111-1111-4111-8111-111111111111",
                        "schema": {"mode": "observed"},
                    },
                }
            },
            "pending_source_intents": {},
        }
    }
    sources_out, meta_out = redact_guided_snapshot_storage_paths(sources, composer_meta)
    assert sources_out["source"]["options"]["path"] == REDACTED_BLOB_SOURCE_PATH
    reviewed = meta_out["guided_session"]["reviewed_sources"]["11111111-1111-4111-8111-111111111111"]
    assert reviewed["options"]["path"] == REDACTED_BLOB_SOURCE_PATH
    # blob_ref is retained — it is the redaction SIGNAL, not a sensitive value.
    assert reviewed["options"]["blob_ref"] == "11111111-1111-4111-8111-111111111111"
    assert real_path not in str(sources_out)
    assert real_path not in str(meta_out)
    # inputs are not mutated.
    assert sources["source"]["options"]["path"] == real_path
    original = composer_meta["guided_session"]["reviewed_sources"]["11111111-1111-4111-8111-111111111111"]
    assert original["options"]["path"] == real_path


def test_redact_guided_snapshot_leaves_operator_typed_source() -> None:
    """No blob_ref on the snapshot => the path is operator-typed, NOT a blob
    carrier => nothing is redacted on either channel."""
    sources = {"source": {"options": {"path": "/tmp/user.csv"}}}
    composer_meta = {
        "guided_session": {
            "reviewed_sources": {
                "11111111-1111-4111-8111-111111111111": {
                    "name": "source",
                    "options": {"path": "/tmp/user.csv", "schema": {"mode": "observed"}},
                }
            },
            "pending_source_intents": {},
        }
    }
    sources_out, meta_out = redact_guided_snapshot_storage_paths(sources, composer_meta)
    assert sources_out["source"]["options"]["path"] == "/tmp/user.csv"
    reviewed = meta_out["guided_session"]["reviewed_sources"]["11111111-1111-4111-8111-111111111111"]
    assert reviewed["options"]["path"] == "/tmp/user.csv"
    assert REDACTED_BLOB_SOURCE_PATH not in str((sources_out, meta_out))


def test_redact_guided_snapshot_projects_canonical_blob_sentinel_by_exact_name() -> None:
    real_path = "/internal/blobs/session/source.csv"
    sentinel = "blob:11111111-1111-4111-8111-111111111111"
    sources = {"source": {"options": {"path": real_path, "schema": {"mode": "observed"}}}}
    composer_meta = {
        "guided_session": {
            "reviewed_sources": {
                "22222222-2222-4222-8222-222222222222": {
                    "name": "source",
                    "options": {"path": sentinel, "schema": {"mode": "observed"}},
                }
            },
            "pending_source_intents": {},
        },
        "implicit_decisions": {
            "schema_version": 1,
            "entries": [{"path": "source.path", "value": real_path, "category": "source"}],
            "normalization_events": [],
        },
    }

    sources_out, meta_out = redact_guided_snapshot_storage_paths(sources, composer_meta)

    assert sources_out["source"]["options"]["path"] == sentinel
    assert real_path not in str((sources_out, meta_out))
    assert meta_out["implicit_decisions"]["entries"][0]["value"] == sentinel
    assert composer_meta["guided_session"]["reviewed_sources"]["22222222-2222-4222-8222-222222222222"]["options"]["path"] == sentinel


def test_redact_guided_snapshot_projects_plural_canonical_sentinels_by_exact_carrier() -> None:
    first_path = "/internal/blobs/session/first.csv"
    second_path = "/internal/blobs/session/second.csv"
    first_sentinel = "blob:11111111-1111-4111-8111-111111111111"
    second_sentinel = "blob:22222222-2222-4222-8222-222222222222"
    sources = {
        "first": {"options": {"path": first_path}},
        "second": {"options": {"file": second_path}},
    }
    composer_meta = {
        "guided_session": {
            "reviewed_sources": {
                "33333333-3333-4333-8333-333333333333": {
                    "name": "first",
                    "options": {"path": first_sentinel},
                },
                "44444444-4444-4444-8444-444444444444": {
                    "name": "second",
                    "options": {"file": second_sentinel},
                },
            },
            "pending_source_intents": {},
        }
    }

    sources_out, meta_out = redact_guided_snapshot_storage_paths(sources, composer_meta)

    assert sources_out["first"]["options"]["path"] == first_sentinel
    assert sources_out["second"]["options"]["file"] == second_sentinel
    assert first_path not in repr((sources_out, meta_out))
    assert second_path not in repr((sources_out, meta_out))


def test_redact_guided_snapshot_rejects_sentinel_mixed_with_private_file_before_projection() -> None:
    private_path = "/internal/blobs/session/source.csv"
    private_file = "/internal/blobs/secret.csv"
    sentinel = "blob:11111111-1111-4111-8111-111111111111"
    sources = {"source": {"options": {"path": private_path, "file": private_file}}}
    composer_meta = {
        "guided_session": {
            "reviewed_sources": {
                "22222222-2222-4222-8222-222222222222": {
                    "name": "source",
                    "options": {"path": sentinel, "file": private_file},
                }
            },
            "pending_source_intents": {},
        }
    }

    with pytest.raises(AuditIntegrityError, match="mixes public sentinels and private paths"):
        redact_guided_snapshot_storage_paths(sources, composer_meta)

    assert sources["source"]["options"]["file"] == private_file
    reviewed = composer_meta["guided_session"]["reviewed_sources"]["22222222-2222-4222-8222-222222222222"]
    assert reviewed["options"]["file"] == private_file


def test_redact_guided_snapshot_rejects_live_blob_ref_conflicting_with_reviewed_sentinel() -> None:
    reviewed_blob_id = "11111111-1111-4111-8111-111111111111"
    conflicting_blob_id = "33333333-3333-4333-8333-333333333333"
    sources = {
        "source": {
            "options": {
                "path": "/internal/blobs/session/source.csv",
                "blob_ref": conflicting_blob_id,
            }
        }
    }
    composer_meta = {
        "guided_session": {
            "reviewed_sources": {
                "22222222-2222-4222-8222-222222222222": {
                    "name": "source",
                    "options": {"path": f"blob:{reviewed_blob_id}"},
                }
            },
            "pending_source_intents": {},
        }
    }

    with pytest.raises(AuditIntegrityError, match="guided blob source mapping"):
        redact_guided_snapshot_storage_paths(sources, composer_meta)


def test_redact_guided_snapshot_accepts_matching_fork_sentinel_and_blob_ref() -> None:
    blob_id = "11111111-1111-4111-8111-111111111111"
    sentinel = f"blob:{blob_id}"
    real_path = "/internal/blobs/child/source.csv"
    sources = {"source": {"options": {"path": real_path, "blob_ref": blob_id}}}
    composer_meta = {
        "guided_session": {
            "reviewed_sources": {
                "22222222-2222-4222-8222-222222222222": {
                    "name": "source",
                    "options": {"path": sentinel, "blob_ref": blob_id},
                }
            },
            "pending_source_intents": {},
        }
    }

    sources_out, meta_out = redact_guided_snapshot_storage_paths(sources, composer_meta)

    assert sources_out["source"]["options"]["path"] == sentinel
    reviewed = meta_out["guided_session"]["reviewed_sources"]["22222222-2222-4222-8222-222222222222"]
    assert reviewed["options"] == {"path": sentinel, "blob_ref": blob_id}
    assert real_path not in str((sources_out, meta_out))

    composer_meta["guided_session"]["reviewed_sources"]["22222222-2222-4222-8222-222222222222"]["options"]["blob_ref"] = (
        "33333333-3333-4333-8333-333333333333"
    )
    with pytest.raises(AuditIntegrityError):
        redact_guided_snapshot_storage_paths(sources, composer_meta)


@pytest.mark.parametrize(
    ("source_name", "sentinel"),
    [
        ("missing", "blob:11111111-1111-4111-8111-111111111111"),
        ("source", "blob:not-a-uuid"),
    ],
    ids=["missing_name", "invalid_sentinel"],
)
def test_redact_guided_snapshot_rejects_unbound_or_invalid_blob_sentinel(source_name: str, sentinel: str) -> None:
    sources = {"source": {"options": {"path": "/internal/blobs/session/source.csv"}}}
    composer_meta = {
        "guided_session": {
            "reviewed_sources": {
                "22222222-2222-4222-8222-222222222222": {
                    "name": source_name,
                    "options": {"path": sentinel},
                }
            },
            "pending_source_intents": {},
        }
    }

    with pytest.raises(AuditIntegrityError):
        redact_guided_snapshot_storage_paths(sources, composer_meta)


def test_redact_guided_snapshot_rejects_duplicate_reviewed_source_names() -> None:
    sources = {"source": {"options": {"path": "/internal/blobs/session/source.csv"}}}
    composer_meta = {
        "guided_session": {
            "reviewed_sources": {
                stable_id: {
                    "name": "source",
                    "options": {"path": f"blob:{stable_id}"},
                }
                for stable_id in (
                    "11111111-1111-4111-8111-111111111111",
                    "22222222-2222-4222-8222-222222222222",
                )
            },
            "pending_source_intents": {},
        }
    }

    with pytest.raises(AuditIntegrityError, match="names must be unique"):
        redact_guided_snapshot_storage_paths(sources, composer_meta)


def test_redact_guided_snapshot_noop_for_freeform_state() -> None:
    """Freeform state (composer_meta is None, or has no guided_session snapshot) is
    returned unchanged — the helper only acts on a guided blob-backed snapshot."""
    sources = {"source": {"options": {"path": "/some/path.csv", "blob_ref": "x"}}}
    s1, m1 = redact_guided_snapshot_storage_paths(sources, None)
    assert s1 == sources
    assert m1 is None
    s2, m2 = redact_guided_snapshot_storage_paths(sources, {"repair_turns_used": 0})
    assert s2["source"]["options"]["path"] == "/some/path.csv"
    assert m2 == {"repair_turns_used": 0}


def test_redact_guided_snapshot_rejects_malformed_present_guided_session() -> None:
    sources = {"source": {"options": {"path": "/some/path.csv"}}}
    with pytest.raises(ValueError, match="guided_session must be a dict"):
        redact_guided_snapshot_storage_paths(sources, {"guided_session": "not-a-session"})


def test_redact_guided_snapshot_rejects_malformed_present_snapshot_options() -> None:
    sources = {"source": {"options": {"path": "/some/path.csv"}}}
    composer_meta = {
        "guided_session": {
            "reviewed_sources": {"11111111-1111-4111-8111-111111111111": {"name": "source", "options": "not-options"}},
            "pending_source_intents": {},
        }
    }
    with pytest.raises(ValueError, match=r"reviewed_sources.*options must be a dict"):
        redact_guided_snapshot_storage_paths(sources, composer_meta)


def test_redact_guided_snapshot_requires_schema8_pending_source_intents() -> None:
    with pytest.raises(KeyError, match="pending_source_intents"):
        redact_guided_snapshot_storage_paths(
            {},
            {"guided_session": {"reviewed_sources": {}}},
        )


def test_redact_guided_snapshot_requires_exact_pending_intent_options() -> None:
    composer_meta = {
        "guided_session": {
            "reviewed_sources": {},
            "pending_source_intents": {
                "11111111-1111-4111-8111-111111111111": {"name": "incoming"},
            },
        }
    }
    with pytest.raises(KeyError, match="options"):
        redact_guided_snapshot_storage_paths({}, composer_meta)


def test_redact_guided_snapshot_rejects_malformed_source_when_blob_redaction_active() -> None:
    real_path = "/home/u/elspeth/data/blobs/sess/abc_data.csv"
    sources = {"source": {"options": "not-options"}}
    composer_meta = {
        "guided_session": {
            "reviewed_sources": {
                "11111111-1111-4111-8111-111111111111": {
                    "name": "source",
                    "options": {"path": real_path, "blob_ref": "11111111-1111-4111-8111-111111111111"},
                }
            },
            "pending_source_intents": {},
        }
    }
    with pytest.raises(ValueError, match=r"source\.options must be a dict"):
        redact_guided_snapshot_storage_paths(sources, composer_meta)


def test_redact_guided_snapshot_masks_file_carrier() -> None:
    """``file`` is an equivalent storage-path carrier to ``path`` (elspeth-a7aa07b7ce);
    the guided snapshot helper masks it on both channels too."""
    real = "/internal/blobs/sess/zzz_data.csv"
    sources = {"source": {"options": {"file": real}}}
    composer_meta = {
        "guided_session": {
            "reviewed_sources": {
                "11111111-1111-4111-8111-111111111111": {
                    "name": "source",
                    "options": {"file": real, "blob_ref": "11111111-1111-4111-8111-111111111111"},
                }
            },
            "pending_source_intents": {},
        }
    }
    sources_out, meta_out = redact_guided_snapshot_storage_paths(sources, composer_meta)
    assert sources_out["source"]["options"]["file"] == REDACTED_BLOB_SOURCE_PATH
    reviewed = meta_out["guided_session"]["reviewed_sources"]["11111111-1111-4111-8111-111111111111"]
    assert reviewed["options"]["file"] == REDACTED_BLOB_SOURCE_PATH
    assert real not in str((sources_out, meta_out))


def test_redact_guided_snapshot_handles_plural_reviewed_sources_by_name() -> None:
    first_path = "/internal/blobs/first.csv"
    second_path = "/internal/blobs/second.csv"
    sources = {
        "first": {"options": {"path": first_path}},
        "second": {"options": {"path": second_path}},
    }
    composer_meta = {
        "guided_session": {
            "reviewed_sources": {
                "11111111-1111-4111-8111-111111111111": {
                    "name": "first",
                    "options": {"path": first_path, "blob_ref": "11111111-1111-4111-8111-111111111111"},
                },
                "22222222-2222-4222-8222-222222222222": {
                    "name": "second",
                    "options": {"path": second_path, "blob_ref": "22222222-2222-4222-8222-222222222222"},
                },
            },
            "pending_source_intents": {},
        }
    }

    sources_out, meta_out = redact_guided_snapshot_storage_paths(sources, composer_meta)

    assert sources_out["first"]["options"]["path"] == REDACTED_BLOB_SOURCE_PATH
    assert sources_out["second"]["options"]["path"] == REDACTED_BLOB_SOURCE_PATH
    reviewed = meta_out["guided_session"]["reviewed_sources"]
    assert reviewed["11111111-1111-4111-8111-111111111111"]["options"]["path"] == REDACTED_BLOB_SOURCE_PATH
    assert reviewed["22222222-2222-4222-8222-222222222222"]["options"]["path"] == REDACTED_BLOB_SOURCE_PATH
    assert first_path not in str((sources_out, meta_out))
    assert second_path not in str((sources_out, meta_out))


def test_redact_guided_snapshot_allows_two_reviewed_names_to_share_one_blob_path() -> None:
    shared_path = "/internal/blobs/shared.csv"
    sources = {
        "first": {"options": {"path": shared_path}},
        "second": {"options": {"path": shared_path}},
    }
    composer_meta = {
        "guided_session": {
            "reviewed_sources": {
                stable_id: {
                    "name": name,
                    "options": {"path": shared_path, "blob_ref": "abc12300-0000-4000-8000-000000000000"},
                }
                for stable_id, name in (
                    ("11111111-1111-4111-8111-111111111111", "first"),
                    ("22222222-2222-4222-8222-222222222222", "second"),
                )
            },
            "pending_source_intents": {},
        }
    }

    sources_out, meta_out = redact_guided_snapshot_storage_paths(sources, composer_meta)

    assert sources_out is not None
    assert sources_out["first"]["options"]["path"] == REDACTED_BLOB_SOURCE_PATH
    assert sources_out["second"]["options"]["path"] == REDACTED_BLOB_SOURCE_PATH
    assert shared_path not in str((sources_out, meta_out))


@pytest.mark.parametrize(
    "invalid_blob_ref",
    [None, "", 123, "98B1357D-5AAB-4FB3-85B4-5AD643912E84"],
    ids=["none", "empty", "wrong_type", "noncanonical_uuid"],
)
def test_redact_guided_snapshot_rejects_present_invalid_reviewed_blob_ref(invalid_blob_ref: object) -> None:
    stable_id = "11111111-1111-4111-8111-111111111111"
    live_path = "/internal/blobs/foreign.csv"
    sources = {"source": {"options": {"path": live_path}}}
    composer_meta = {
        "guided_session": {
            "reviewed_sources": {
                stable_id: {
                    "name": "source",
                    "options": {"path": live_path, "blob_ref": invalid_blob_ref},
                }
            },
            "pending_source_intents": {},
        }
    }

    with pytest.raises(AuditIntegrityError, match="canonical UUID"):
        redact_guided_snapshot_storage_paths(sources, composer_meta)

    assert sources["source"]["options"]["path"] == live_path
    assert composer_meta["guided_session"]["reviewed_sources"][stable_id]["options"] == {
        "path": live_path,
        "blob_ref": invalid_blob_ref,
    }


@pytest.mark.parametrize(
    "invalid_carriers",
    [
        {"path": ""},
        {"file": ""},
        {"path": None},
        {"file": 123},
        {"path": "/internal/blobs/foreign.csv", "file": None},
        {"path": "/internal/blobs/for\x00eign.csv"},
    ],
    ids=["empty_path", "empty_file", "none_path", "wrong_type_file", "valid_path_invalid_file", "nul_path"],
)
def test_redact_guided_snapshot_rejects_invalid_reviewed_path_carriers(invalid_carriers: dict[str, object]) -> None:
    stable_id = "11111111-1111-4111-8111-111111111111"
    live_path = "/internal/blobs/foreign.csv"
    sources = {"source": {"options": {"path": live_path}}}
    snapshot_options = {**invalid_carriers, "blob_ref": stable_id}
    composer_meta = {
        "guided_session": {
            "reviewed_sources": {
                stable_id: {
                    "name": "source",
                    "options": snapshot_options,
                }
            },
            "pending_source_intents": {},
        }
    }

    with pytest.raises(AuditIntegrityError, match="path carrier"):
        redact_guided_snapshot_storage_paths(sources, composer_meta)

    assert sources["source"]["options"]["path"] == live_path
    assert composer_meta["guided_session"]["reviewed_sources"][stable_id]["options"] == snapshot_options


def test_redact_guided_snapshot_accepts_two_valid_path_carriers() -> None:
    stable_id = "11111111-1111-4111-8111-111111111111"
    path = "/internal/blobs/source.csv"
    file = "/internal/blobs/source-alias.csv"
    sources = {"source": {"options": {"path": path, "file": file}}}
    composer_meta = {
        "guided_session": {
            "reviewed_sources": {
                stable_id: {
                    "name": "source",
                    "options": {"path": path, "file": file, "blob_ref": stable_id},
                }
            },
            "pending_source_intents": {},
        }
    }

    sources_out, meta_out = redact_guided_snapshot_storage_paths(sources, composer_meta)

    assert sources_out["source"]["options"]["path"] == REDACTED_BLOB_SOURCE_PATH
    assert sources_out["source"]["options"]["file"] == REDACTED_BLOB_SOURCE_PATH
    reviewed_options = meta_out["guided_session"]["reviewed_sources"][stable_id]["options"]
    assert reviewed_options["path"] == REDACTED_BLOB_SOURCE_PATH
    assert reviewed_options["file"] == REDACTED_BLOB_SOURCE_PATH


@pytest.mark.parametrize(
    ("reviewed_carriers", "live_options"),
    [
        ({"path": " /internal/blobs/bogus.csv "}, {"path": "/internal/blobs/live.csv"}),
        ({"path": " /internal/blobs/bogus.csv "}, {"schema": {"mode": "observed"}}),
        ({"path": "/internal/blobs/source.csv"}, {"path": "/internal/blobs/source.csv", "file": "/internal/blobs/secret.csv"}),
        ({"path": "/internal/blobs/source.csv", "file": "/internal/blobs/source-alias.csv"}, {"path": "/internal/blobs/source.csv"}),
    ],
    ids=["mismatched_path", "missing_live_carrier", "extra_live_carrier", "missing_live_reviewed_carrier"],
)
def test_redact_guided_snapshot_rejects_same_name_without_exact_reviewed_path(
    reviewed_carriers: dict[str, object],
    live_options: dict[str, object],
) -> None:
    stable_id = "11111111-1111-4111-8111-111111111111"
    sources = {"source": {"options": live_options}}
    composer_meta = {
        "guided_session": {
            "reviewed_sources": {
                stable_id: {
                    "name": "source",
                    "options": {**reviewed_carriers, "blob_ref": stable_id},
                }
            },
            "pending_source_intents": {},
        }
    }

    with pytest.raises(AuditIntegrityError, match="guided blob source mapping"):
        redact_guided_snapshot_storage_paths(sources, composer_meta)

    assert sources["source"]["options"] == live_options
    assert composer_meta["guided_session"]["reviewed_sources"][stable_id]["options"] == {
        **reviewed_carriers,
        "blob_ref": stable_id,
    }


def test_redact_guided_snapshot_rejects_reviewed_blob_ref_without_string_path_carrier() -> None:
    """A reviewed blob binding without its path cannot be mapped safely."""
    stable_id = "11111111-1111-4111-8111-111111111111"
    live_path = "/internal/blobs/foreign.csv"
    sources = {"source": {"options": {"path": live_path}}}
    composer_meta = {
        "guided_session": {
            "reviewed_sources": {
                stable_id: {
                    "name": "source",
                    "options": {"blob_ref": stable_id},
                }
            },
            "pending_source_intents": {},
        }
    }

    with pytest.raises(AuditIntegrityError, match="string path carrier"):
        redact_guided_snapshot_storage_paths(sources, composer_meta)

    assert sources["source"]["options"]["path"] == live_path
    assert composer_meta["guided_session"]["reviewed_sources"][stable_id]["options"] == {"blob_ref": stable_id}


def test_redact_guided_snapshot_fails_closed_when_name_drift_hides_same_blob_path() -> None:
    real_path = "/internal/blobs/renamed.csv"
    sources = {"renamed": {"options": {"path": real_path}}}
    composer_meta = {
        "guided_session": {
            "reviewed_sources": {
                "11111111-1111-4111-8111-111111111111": {
                    "name": "original",
                    "options": {"path": real_path, "blob_ref": "11111111-1111-4111-8111-111111111111"},
                }
            },
            "pending_source_intents": {},
        }
    }

    with pytest.raises(AuditIntegrityError, match="guided blob source mapping"):
        redact_guided_snapshot_storage_paths(sources, composer_meta)

    assert sources["renamed"]["options"]["path"] == real_path
    snapshot = composer_meta["guided_session"]["reviewed_sources"]["11111111-1111-4111-8111-111111111111"]
    assert snapshot["options"]["path"] == real_path


@pytest.mark.parametrize("carrier", ["path", "file"])
def test_redact_guided_pending_source_intent_blob_path_without_mutation(carrier: str) -> None:
    real_path = f"/internal/blobs/pending-{carrier}.csv"
    pending_id = "11111111-1111-4111-8111-111111111111"
    sources = {"current": {"options": {"path": "/operator/current.csv"}}}
    composer_meta = {
        "guided_session": {
            "reviewed_sources": {},
            "pending_source_intents": {
                pending_id: {
                    "name": "incoming",
                    "phase": "inspection_review",
                    "plugin": "csv",
                    "options": {carrier: real_path, "blob_ref": "11111111-1111-4111-8111-111111111111"},
                    "inspection_facts": None,
                    "observed_columns": [],
                    "sample_rows": [],
                }
            },
        }
    }

    sources_out, meta_out = redact_guided_snapshot_storage_paths(sources, composer_meta)

    assert sources_out == sources
    assert meta_out is not None
    pending = meta_out["guided_session"]["pending_source_intents"][pending_id]
    assert pending["options"][carrier] == REDACTED_BLOB_SOURCE_PATH
    assert pending["options"]["blob_ref"] == "11111111-1111-4111-8111-111111111111"
    assert real_path not in str(meta_out)
    assert composer_meta["guided_session"]["pending_source_intents"][pending_id]["options"][carrier] == real_path


def test_summarize_set_source_options_accepts_coerced_datetime() -> None:
    """Pin rev-3 A7: summarizer MUST NOT raise on reachable input values.

    Spec §9 RSK-03 requires the summarizer not raise on any reachable
    input value.  Pydantic 2.x can coerce string-like inputs to
    :class:`datetime` when the field accepts ``Any``; :func:`json.dumps`
    raises :class:`TypeError` on ``datetime`` unless ``default=str`` is
    supplied.  This test pins the ``default=str`` argument so a future
    refactor that removes it fails loudly here rather than silently
    violating RSK-03.
    """
    options = {"since": datetime(2026, 1, 1, tzinfo=UTC), "key": "v"}
    result = _summarize_set_source_options(options)
    assert isinstance(result, str)


def test_summarize_set_source_options_never_serializes_untrusted_key_names() -> None:
    """Open option keys are data, not trusted audit-schema field names."""
    secret_key = "api-key=SUPER-SECRET-CANARY"
    nested_key = "nested-secret-key=PROMPT-INJECTION-CANARY"
    unicode_key = "秘密🔐キー"
    long_key = "LONG-KEY-CANARY-" + ("x" * 20_000)
    options = {
        secret_key: {nested_key: "value"},
        unicode_key: ["first", "second"],
        long_key: {"set-member-a", "set-member-b"},
    }
    equivalent_shape = {
        "different-mapping-key": {"different-nested-key": "different-value"},
        "different-sequence-key": [1, 2, 3, 4],
        "different-set-key": {1},
    }

    summary = _summarize_set_source_options(options)

    assert json.loads(summary) == _option_shape_summary(mapping=1, sequence=1, set_=1)
    assert summary == _summarize_set_source_options(equivalent_shape)
    assert len(summary) < 256
    for canary in (secret_key, nested_key, unicode_key, long_key, "set-member-a"):
        assert canary not in summary


_CANARY = "CANARY-SENSITIVE-PATH-DO-NOT-LEAK"


def test_serialization_boundary_canary_not_in_json_output() -> None:
    """Pin the Phase 3 cross-boundary integration contract (rev-2 BLOCKER_A).

    Phase 3 passes the result of :func:`redact_tool_call_arguments` through
    :func:`json.dumps` before writing to ``chat_messages.tool_calls``.  This
    test verifies the canary never survives that serialization — even
    though :func:`json.dumps` would otherwise re-emit the canary if it
    appeared anywhere in the dict. The source-option summarizer substitutes
    scalar option values before serialization, independent of blob_ref.
    """
    args = {
        "plugin": "csv",
        "options": {"path": _CANARY, "blob_ref": "abc123"},
        "on_success": "rows",
        "on_validation_failure": "discard",
    }
    result = redact_tool_call_arguments("set_source", args, telemetry=NoopRedactionTelemetry())
    serialized = json.dumps(result, sort_keys=True)
    assert _CANARY not in serialized, (
        "Sensitive canary value appeared in serialized output. "
        "Redaction did not remove it from the persistence path. "
        f"Serialized: {serialized!r}"
    )
    assert "options" in serialized  # key preserved, value redacted


# ---------------------------------------------------------------------------
# Task-7 boundary tests: no-summarizer → sentinel; nested-path → NotImplementedError
# ---------------------------------------------------------------------------


def test_redact_via_schema_substitutes_sentinel_for_sensitive_field_without_summarizer() -> None:
    """Task-7: Sensitive field with no summarizer receives REDACTED_SENSITIVE_NO_SUMMARIZER.

    The Task-4 tracer-bullet raised ``NotImplementedError`` here to force
    Task 8 to define the policy.  Task 7 defines the policy: substitute the
    no-summarizer sentinel rather than preserving the raw value.  Task 8 will
    generalise nested-path handling; this test pins the top-level case.
    """
    from elspeth.web.composer.redaction import REDACTED_SENSITIVE_NO_SUMMARIZER

    class _StubModel(BaseModel):
        secret: Annotated[str, Sensitive()]  # no summarizer

    validated = _StubModel.model_validate({"secret": "CANARY"})
    tel = NoopRedactionTelemetry()
    result = _redact_via_schema("stub_tool", validated, _StubModel, telemetry=tel)
    assert result["secret"] == REDACTED_SENSITIVE_NO_SUMMARIZER
    assert "CANARY" not in str(result.values())


def test_redact_via_schema_substitutes_nested_sensitive_path() -> None:
    """Task-8 generalisation: nested-path Sensitive field is substituted in-place.

    Task 4's tracer-bullet raised ``NotImplementedError`` for any path
    containing ``.``, ``[``, or ``{``; Task 8 supersedes that boundary by
    implementing the per-path substitute closure on ``TraversalNode``. The
    inner field's summarizer output replaces the value at the nested location
    while the surrounding structure is preserved.
    """

    class _InnerModel(BaseModel):
        inner_secret: Annotated[str, Sensitive(summarizer=lambda v: "<fixed-sum>")]
        public_field: str

    class _OuterModel(BaseModel):
        payload: _InnerModel

    validated = _OuterModel.model_validate({"payload": {"inner_secret": "RAW_SECRET", "public_field": "shown"}})
    tel = NoopRedactionTelemetry()
    result = _redact_via_schema("stub_tool", validated, _OuterModel, telemetry=tel)
    assert result["payload"]["inner_secret"] == "<fixed-sum>"
    assert result["payload"]["public_field"] == "shown"
    assert "RAW_SECRET" not in str(result)


# ---------------------------------------------------------------------------
# Tier-model burn-down (B36) pins: every guard below was rewritten from a
# ``.get()`` / ABC ``isinstance`` form to a nominal ``type() is dict`` or
# membership-form read. These tests hold the redaction behaviour fixed across
# that rewrite — a value that MUST be redacted still is, and a malformed
# first-party shape fails closed instead of passing an un-redacted path.
# ---------------------------------------------------------------------------


def test_redact_source_storage_path_rejects_non_dict_source_shape() -> None:
    """A present, non-dict source is a corrupted Tier-1 serializer output.

    ``_redact_one`` checks ``type(source) is dict`` nominally: every producer
    feeding this surface (``CompositionState.to_dict``, ``deep_thaw`` in the
    session routes, the JSON-decoded MCP result) emits plain dicts, so even a
    read-only ``Mapping`` here is a shape nothing first-party produces.
    """
    from types import MappingProxyType

    proxied = MappingProxyType({"options": {"path": "/internal/blob/x.csv", "blob_ref": "abc"}})
    with pytest.raises(AuditIntegrityError, match="non-Mapping source value"):
        redact_source_storage_path({"source": proxied})
    with pytest.raises(AuditIntegrityError, match="non-Mapping source value"):
        redact_source_storage_path({"source": "not-a-mapping"})


def test_redact_source_storage_path_rejects_non_dict_options_carrying_blob_path() -> None:
    """A non-dict ``options`` value must fail closed, never pass through.

    Before the burn-down a non-``Mapping`` options value returned the source
    unchanged; a read-only ``Mapping`` was redacted. Both now raise: silently
    returning a malformed options carrier is exactly the leak this surface
    exists to prevent, and the private path must not appear in the error.
    """
    from types import MappingProxyType

    private_path = "/internal/blob/secret-storage.csv"
    proxied_options = MappingProxyType({"path": private_path, "blob_ref": "abc"})
    with pytest.raises(AuditIntegrityError, match=r"non-dict source\.options") as excinfo:
        redact_source_storage_path({"source": {"options": proxied_options}})
    assert private_path not in str(excinfo.value)
    with pytest.raises(AuditIntegrityError, match=r"non-dict source\.options"):
        redact_source_storage_path({"sources": {"s": {"options": [private_path, "blob_ref"]}}})


def test_redact_source_storage_path_none_options_and_missing_blob_ref_pass_through() -> None:
    """The documented first-party no-op shapes are unchanged by the rewrite."""
    states: list[dict[str, Any]] = [
        {"source": {"options": None}},
        {"source": {}},
        {"source": None},
        {"source": {"options": {"path": "/tmp/user.csv"}}},
    ]
    for state in states:
        assert redact_source_storage_path(state) == state


def test_coerce_stringified_json_object_never_raises_on_hostile_text() -> None:
    """``_coerce_stringified_json_object`` is an observation boundary: it never raises.

    Depth is bounded by ``bounded_json_loads`` (``RecursionError`` becomes a
    ``JsonBoundaryError``, a ``ValueError``), so the previously documented
    unbounded-recursion exposure is closed and every non-object outcome is
    returned untouched for pydantic to reject.
    """
    deep = "[" * 20_000
    assert _coerce_stringified_json_object(deep) is deep
    for untouched in ("not json", "[1, 2]", "null", '"str"', "42", 7, None, ["x"], {"already": "dict"}):
        assert _coerce_stringified_json_object(untouched) is untouched
    assert _coerce_stringified_json_object('{"k": 1}') == {"k": 1}


def test_normalize_set_pipeline_redacted_arguments_membership_shapes() -> None:
    """Only ``source.inline_blob is None`` is dropped; every other shape is untouched."""
    assert normalize_set_pipeline_redacted_arguments("scalar") == "scalar"
    no_source: dict[str, Any] = {"nodes": []}
    assert normalize_set_pipeline_redacted_arguments(no_source) is no_source
    non_dict_source = {"source": ["x"]}
    assert normalize_set_pipeline_redacted_arguments(non_dict_source) is non_dict_source
    absent = {"source": {"plugin": "csv"}}
    assert normalize_set_pipeline_redacted_arguments(absent) is absent
    present = {"source": {"plugin": "csv", "inline_blob": "<redacted>"}}
    assert normalize_set_pipeline_redacted_arguments(present) is present
    null_blob = {"source": {"plugin": "csv", "inline_blob": None}, "nodes": []}
    normalized = normalize_set_pipeline_redacted_arguments(null_blob)
    assert normalized == {"source": {"plugin": "csv"}, "nodes": []}
    assert null_blob["source"] == {"plugin": "csv", "inline_blob": None}


def _frozen_set_pipeline_arguments(source_block: dict[str, Any]) -> Mapping[str, Any]:
    """Return a set_pipeline-shaped mapping frozen by a real freezing producer.

    ``PipelineProposal.__post_init__`` deep-freezes ``pipeline``, so the
    mapping and every nested block come back as ``mappingproxy``. Building the
    frozen form here rather than calling ``deep_freeze`` on a literal is what
    makes the pins below fail if that authority ever stops freezing — a
    literal would stay green and prove nothing.

    It is the NEAREST real producer rather than the owner of this exact value:
    nothing in the tree freezes the *redacted* projection, which reaches the
    normaliser through ``json.loads`` / ``redact_tool_call_arguments``. What
    the proposal contributes is a genuinely frozen set_pipeline-shaped
    mapping, which is the input class under test.
    """
    proposal = PipelineProposal.create(
        pipeline={"source": source_block, "nodes": []},
        base=AbsentBase(),
        reviewed_facts={},
        surface=PlannerSurface.GUIDED_FULL,
        repair_count=0,
        skill_hash=stable_hash("planner-skill"),
        covered_deferred_intent_ids=(),
        supersedes_draft_hash=None,
    )
    frozen = proposal.pipeline
    assert type(frozen) is MappingProxyType
    assert type(frozen["source"]) is MappingProxyType
    return frozen


def test_normalize_set_pipeline_redacted_arguments_reads_the_frozen_authority_form() -> None:
    """Frozen and thawed spellings of one proposal must normalise identically.

    ``normalize_set_pipeline_redacted_arguments`` answers "nothing to
    normalise" by returning its argument unchanged, so a mapping it fails to
    RECOGNISE is indistinguishable from one that needed no work — the two
    spellings of "no inline blob" then persist as different redacted authority
    projections. ``_create_composition_proposal`` compares that projection to
    the manifest's and raises ``AuditIntegrityError`` on a mismatch, and
    ``ComposerToolInvocation`` banks ``semantic_arguments_hash =
    stored_authority_hash`` whenever the normaliser returned its input
    identically — so an unrecognised frozen mapping is banked under a hash for
    a projection that was never produced.
    """
    null_blob_source: dict[str, Any] = {"plugin": "csv", "inline_blob": None}
    frozen = _frozen_set_pipeline_arguments(null_blob_source)
    thawed = deep_thaw(frozen)

    from_frozen = normalize_set_pipeline_redacted_arguments(frozen)
    from_thawed = normalize_set_pipeline_redacted_arguments(thawed)

    # Normalise-then-thaw and thaw-then-normalise must commute: the frozen and
    # thawed spellings of one authority carry the same redacted projection.
    # (The frozen result keeps its frozen carriers — ``nodes`` stays a tuple —
    # so the comparison is on thawed content, not container identity.)
    assert from_thawed == {"source": {"plugin": "csv"}, "nodes": []}
    assert deep_thaw(from_frozen) == from_thawed
    # The nested arm is the reachable one: a shallow thaw of the authority
    # leaves a real outer dict whose ``source`` is still frozen, which passes
    # ComposerToolInvocation's outer exact-dict reject-gate untouched.
    shallow = dict(frozen)
    assert type(shallow["source"]) is MappingProxyType
    assert deep_thaw(normalize_set_pipeline_redacted_arguments(shallow)) == from_thawed


def test_normalize_set_pipeline_redacted_arguments_leaves_a_frozen_redacted_blob_alone() -> None:
    """The untouched arm: recognising the frozen form must not drop a real blob."""
    redacted_source: dict[str, Any] = {"plugin": "csv", "inline_blob": "<redacted>"}
    frozen = _frozen_set_pipeline_arguments(redacted_source)

    assert normalize_set_pipeline_redacted_arguments(frozen) is frozen
    shallow = dict(frozen)
    assert normalize_set_pipeline_redacted_arguments(shallow) is shallow
    assert normalize_set_pipeline_redacted_arguments(deep_thaw(frozen)) == {
        "source": {"plugin": "csv", "inline_blob": "<redacted>"},
        "nodes": [],
    }


def _sentinel_projection_meta(real_path: str, sentinel: str, entries: object) -> dict[str, Any]:
    return {
        "guided_session": {
            "reviewed_sources": {
                "22222222-2222-4222-8222-222222222222": {
                    "name": "source",
                    "options": {"path": sentinel, "schema": {"mode": "observed"}},
                }
            },
            "pending_source_intents": {},
        },
        "implicit_decisions": {"schema_version": 1, "entries": entries, "normalization_events": []},
    }


def test_redact_guided_snapshot_implicit_decision_entries_use_membership_reads() -> None:
    """Entries lacking ``path``/``value``, or with a non-projected value, are untouched.

    The projection lookup is membership-form on the owned
    ``private_path_projections`` dict; a private path that IS projected must
    still be replaced by its sentinel, and nothing else in the entry changes.
    """
    real_path = "/internal/blobs/session/source.csv"
    sentinel = "blob:11111111-1111-4111-8111-111111111111"
    sources = {"source": {"options": {"path": real_path, "schema": {"mode": "observed"}}}}
    entries = [
        {"path": "source.path", "value": real_path, "category": "source"},
        {"path": "source.file", "value": "/tmp/other.csv", "category": "source"},
        {"path": "source.path", "category": "source"},
        {"value": real_path, "category": "source"},
        {"path": "source.path", "value": 3, "category": "source"},
    ]
    _sources_out, meta_out = redact_guided_snapshot_storage_paths(sources, _sentinel_projection_meta(real_path, sentinel, entries))
    assert meta_out is not None
    projected = meta_out["implicit_decisions"]["entries"]
    assert projected[0] == {"path": "source.path", "value": sentinel, "category": "source"}
    assert projected[1:] == entries[1:]
    assert real_path not in str(projected[0])


def test_redact_guided_snapshot_implicit_decision_report_without_entries_fails_closed() -> None:
    real_path = "/internal/blobs/session/source.csv"
    sentinel = "blob:11111111-1111-4111-8111-111111111111"
    sources = {"source": {"options": {"path": real_path, "schema": {"mode": "observed"}}}}
    meta = _sentinel_projection_meta(real_path, sentinel, [])
    del meta["implicit_decisions"]["entries"]
    with pytest.raises(AuditIntegrityError, match="implicit-decision projection is malformed"):
        redact_guided_snapshot_storage_paths(sources, meta)


_FORK_EXPLICIT_BLOB_REF = "50f5b3e9-f52f-4c5f-98df-a20ec7b2627b"
_FORK_EXPLICIT_PATH = "/srv/elspeth/data/blobs/child/50f5b3e9_colours.csv"


def _fork_explicit_shape() -> tuple[dict[str, Any], dict[str, Any]]:
    """Fork-rehydrated explicit-blob_ref shape: reviewed snapshot and live source
    both carry the SAME private path and the SAME blob_ref (elspeth-75d320fb25)."""
    sources = {"source": {"plugin": "csv", "options": {"path": _FORK_EXPLICIT_PATH, "blob_ref": _FORK_EXPLICIT_BLOB_REF}}}
    composer_meta = {
        "guided_session": {
            "reviewed_sources": {
                "11111111-1111-4111-8111-111111111111": {
                    "name": "source",
                    "plugin": "csv",
                    "options": {"path": _FORK_EXPLICIT_PATH, "blob_ref": _FORK_EXPLICIT_BLOB_REF},
                }
            },
            "pending_source_intents": {},
        }
    }
    return sources, composer_meta


def test_redact_guided_snapshot_correlates_on_raw_sources_after_generic_redaction() -> None:
    """Projection order (elspeth-75d320fb25): ``redact_source_storage_path`` runs
    first and masks the live carrier, so a correlation on the generic-redacted copy
    compares the reviewed private path against the redacted literal and raises.
    Passing the raw sources as ``raw_sources`` correlates on the persisted values
    and applies the guided masks onto the generic-redacted copy."""
    raw_sources, composer_meta = _fork_explicit_shape()
    generic_sources = redact_source_storage_path({"sources": raw_sources})["sources"]
    assert generic_sources["source"]["options"]["path"] == REDACTED_BLOB_SOURCE_PATH

    raw_out, raw_meta = redact_guided_snapshot_storage_paths(raw_sources, composer_meta)
    projected_out, projected_meta = redact_guided_snapshot_storage_paths(generic_sources, composer_meta, raw_sources=raw_sources)

    assert projected_out == raw_out
    assert projected_meta == raw_meta
    assert projected_out["source"]["options"] == {"path": REDACTED_BLOB_SOURCE_PATH, "blob_ref": _FORK_EXPLICIT_BLOB_REF}
    assert _FORK_EXPLICIT_PATH not in str((projected_out, projected_meta))
    assert raw_sources["source"]["options"]["path"] == _FORK_EXPLICIT_PATH


def test_redact_guided_snapshot_projection_order_without_raw_sources_still_raises() -> None:
    """The defect shape stays a raise when the caller withholds the raw sources:
    the generic-redacted copy carries no reviewed path to correlate on."""
    raw_sources, composer_meta = _fork_explicit_shape()
    generic_sources = redact_source_storage_path({"sources": raw_sources})["sources"]
    with pytest.raises(AuditIntegrityError, match="guided blob source mapping"):
        redact_guided_snapshot_storage_paths(generic_sources, composer_meta)


def test_redact_guided_snapshot_raw_correlation_stamps_sentinel_over_generic_mask() -> None:
    """Fork sentinel shape: the live source carries blob_ref (generic masks it) and
    the reviewed snapshot carries the sentinel. The sentinel is projected, exactly as
    the pre-raw-correlation order produced."""
    blob_id = "11111111-1111-4111-8111-111111111111"
    sentinel = f"blob:{blob_id}"
    real_path = "/internal/blobs/child/source.csv"
    raw_sources = {"source": {"options": {"path": real_path, "blob_ref": blob_id}}}
    composer_meta = {
        "guided_session": {
            "reviewed_sources": {
                "22222222-2222-4222-8222-222222222222": {"name": "source", "options": {"path": sentinel, "blob_ref": blob_id}}
            },
            "pending_source_intents": {},
        }
    }
    composer_meta["implicit_decisions"] = {
        "schema_version": 1,
        "entries": [{"path": "source.path", "value": real_path, "category": "source"}],
        "normalization_events": [],
    }
    generic_sources = redact_source_storage_path({"sources": raw_sources})["sources"]

    sources_out, meta_out = redact_guided_snapshot_storage_paths(generic_sources, composer_meta, raw_sources=raw_sources)

    assert sources_out["source"]["options"] == {"path": sentinel, "blob_ref": blob_id}
    # The raw-path correlation also masks the implicit-decision echo of the raw
    # carrier value — at base the projection map was keyed on the generic
    # literal, so this entry leaked the private path (accepted leak fix,
    # fix round 1 F-A5).
    assert meta_out["implicit_decisions"]["entries"][0]["value"] == sentinel
    assert real_path not in str((sources_out, meta_out))


def test_redact_guided_snapshot_rejects_raw_sources_that_disagree_in_shape() -> None:
    raw_sources, composer_meta = _fork_explicit_shape()
    with pytest.raises(AuditIntegrityError, match="raw_sources"):
        redact_guided_snapshot_storage_paths({"other": raw_sources["source"]}, composer_meta, raw_sources=raw_sources)


_EXITED_TERMINAL = {"kind": "exited_to_freeform", "reason": "user_pressed_exit", "pipeline_yaml": None}
_COMPLETED_TERMINAL = {"kind": "completed", "reason": None, "pipeline_yaml": "sources: {}\n"}
_TERMINALS = pytest.mark.parametrize("terminal", [_EXITED_TERMINAL, _COMPLETED_TERMINAL], ids=["exited", "completed"])

_PRIVATE_A = "/srv/elspeth/data/blobs/s1/aaaaaaaa-0000-4000-8000-000000000001_a.csv"
_PRIVATE_B = "/srv/elspeth/data/blobs/s1/bbbbbbbb-0000-4000-8000-000000000002_b.csv"
_REPOINTED_BLOB_REF = "cccccccc-0000-4000-8000-000000000003"
_PRIVATE_REPOINTED = f"/srv/elspeth/data/blobs/s1/{_REPOINTED_BLOB_REF}_c.csv"


def _two_guided_committed_sources_repointed_after_exit(terminal: object) -> tuple[dict[str, Any], dict[str, Any]]:
    """Two guided-committed sentinel-form reviewed sources ``a`` and ``b``; both live
    sources carry the private path with NO blob_ref (guided set_source strips it);
    after the terminal, freeform re-pointed ``b`` at a different blob (explicit
    blob_ref). The strict binding fails on ``b``; ``a`` still carries its private
    path and is masked today only by the sentinel-stamping arm that never runs
    once the function raises (adversary Critical 1)."""
    sources = {
        "a": {"plugin": "csv", "options": {"path": _PRIVATE_A}},
        "b": {"plugin": "csv", "options": {"path": _PRIVATE_REPOINTED, "blob_ref": _REPOINTED_BLOB_REF}},
    }
    composer_meta = {
        "guided_session": {
            "reviewed_sources": {
                "11111111-1111-4111-8111-111111111111": {
                    "name": "a",
                    "plugin": "csv",
                    "options": {"path": "blob:aaaaaaaa-0000-4000-8000-000000000001"},
                },
                "22222222-2222-4222-8222-222222222222": {
                    "name": "b",
                    "plugin": "csv",
                    "options": {"path": "blob:bbbbbbbb-0000-4000-8000-000000000002"},
                },
            },
            "pending_source_intents": {
                "33333333-3333-4333-8333-333333333333": {
                    "name": "c",
                    "options": {"file": _PRIVATE_B, "blob_ref": "bbbbbbbb-0000-4000-8000-000000000002"},
                }
            },
            "terminal": terminal,
        },
        "implicit_decisions": {
            "schema_version": 1,
            "entries": [
                {"path": "source.path", "value": _PRIVATE_A, "category": "source"},
                {"path": "source.file", "value": _PRIVATE_B, "category": "source"},
                {"path": "output.path", "value": "outputs/out.jsonl", "category": "output"},
            ],
            "normalization_events": [],
        },
    }
    return sources, composer_meta


def _project(sources: dict[str, Any], composer_meta: dict[str, Any]) -> tuple[Any, Any]:
    generic = redact_source_storage_path({"sources": sources})["sources"]
    return redact_guided_snapshot_storage_paths(generic, composer_meta, raw_sources=sources)


@_TERMINALS
def test_redact_guided_snapshot_terminal_degrades_and_masks_every_carrier(terminal: dict[str, Any]) -> None:
    sources, composer_meta = _two_guided_committed_sources_repointed_after_exit(terminal)

    sources_out, meta_out = _project(sources, composer_meta)

    projected = json.dumps((sources_out, meta_out))
    for private in (_PRIVATE_A, _PRIVATE_B, _PRIVATE_REPOINTED):
        assert private not in projected
    assert "blob:" not in json.dumps(sources_out), "the degraded branch never stamps a sentinel on a live source"
    assert sources_out["a"]["options"]["path"] == REDACTED_BLOB_SOURCE_PATH
    assert sources_out["b"]["options"] == {"path": REDACTED_BLOB_SOURCE_PATH, "blob_ref": _REPOINTED_BLOB_REF}
    guided = meta_out["guided_session"]
    assert guided["custody_unavailable"] is True
    assert guided["terminal"] == terminal
    pending = guided["pending_source_intents"]["33333333-3333-4333-8333-333333333333"]["options"]
    assert pending["file"] == REDACTED_BLOB_SOURCE_PATH
    entries = meta_out["implicit_decisions"]["entries"]
    assert [entry["value"] for entry in entries] == [REDACTED_BLOB_SOURCE_PATH, REDACTED_BLOB_SOURCE_PATH, "outputs/out.jsonl"]
    # Inputs are never mutated: the degraded projection is projection-only.
    assert sources["a"]["options"]["path"] == _PRIVATE_A
    assert "custody_unavailable" not in composer_meta["guided_session"]


def test_redact_guided_snapshot_active_session_still_raises_on_the_degrade_shape() -> None:
    sources, composer_meta = _two_guided_committed_sources_repointed_after_exit(None)
    with pytest.raises(AuditIntegrityError, match="guided blob"):
        _project(sources, composer_meta)


def test_redact_guided_snapshot_active_session_without_terminal_key_still_raises() -> None:
    sources, composer_meta = _two_guided_committed_sources_repointed_after_exit(None)
    del composer_meta["guided_session"]["terminal"]
    with pytest.raises(AuditIntegrityError, match="guided blob"):
        _project(sources, composer_meta)


@_TERMINALS
def test_redact_guided_snapshot_incident_v13_shape_projects_degraded(terminal: dict[str, Any]) -> None:
    """elspeth-201903a286 v13: the retained sentinel review of ``source`` (blob
    360e1583) re-attached to a planner-authored ``source`` bound to blob 50f5b3e9."""
    private = "/srv/elspeth/data/blobs/s1/50f5b3e9-f52f-4c5f-98df-a20ec7b2627b_colours.csv"
    sources = {"source": {"plugin": "csv", "options": {"path": private, "blob_ref": "50f5b3e9-f52f-4c5f-98df-a20ec7b2627b"}}}
    composer_meta = {
        "guided_session": {
            "reviewed_sources": {
                "11111111-1111-4111-8111-111111111111": {
                    "name": "source",
                    "plugin": "csv",
                    "options": {"path": "blob:360e1583-ae3c-4135-9240-0a26a14cf22f"},
                }
            },
            "pending_source_intents": {},
            "terminal": terminal,
        }
    }

    sources_out, meta_out = _project(sources, composer_meta)

    assert private not in json.dumps((sources_out, meta_out))
    assert sources_out["source"]["options"]["path"] == REDACTED_BLOB_SOURCE_PATH
    assert meta_out["guided_session"]["custody_unavailable"] is True


@_TERMINALS
def test_redact_guided_snapshot_bound_terminal_projection_is_byte_identical_to_active(terminal: dict[str, Any]) -> None:
    """Mutation cases D (nothing authored) and E (exact blob reuse) do not raise, so
    a terminal must not change their projection: no ``custody_unavailable`` key,
    output equal to the active-session projection (stored guided_response_hash)."""
    private = "/srv/elspeth/data/blobs/s1/360e1583-ae3c-4135-9240-0a26a14cf22f_colours.csv"
    reviewed = {
        "11111111-1111-4111-8111-111111111111": {
            "name": "source",
            "plugin": "csv",
            "options": {"path": "blob:360e1583-ae3c-4135-9240-0a26a14cf22f"},
        }
    }
    for sources in (
        {},
        {"source": {"plugin": "csv", "options": {"path": private}}},
        {"source": {"plugin": "csv", "options": {"path": private, "blob_ref": "360e1583-ae3c-4135-9240-0a26a14cf22f"}}},
    ):
        active_meta = {"guided_session": {"reviewed_sources": reviewed, "pending_source_intents": {}, "terminal": None}}
        terminal_meta = {"guided_session": {"reviewed_sources": reviewed, "pending_source_intents": {}, "terminal": terminal}}
        active_out = _project(sources, active_meta)
        terminal_out = _project(sources, terminal_meta)
        assert "custody_unavailable" not in terminal_out[1]["guided_session"]
        assert terminal_out[0] == active_out[0]
        assert {k: v for k, v in terminal_out[1]["guided_session"].items() if k != "terminal"} == {
            k: v for k, v in active_out[1]["guided_session"].items() if k != "terminal"
        }


@_TERMINALS
def test_redact_guided_snapshot_case_c_is_out_of_scope_in_terminal_sessions(terminal: dict[str, Any]) -> None:
    """Mutation case C (elspeth-201903a286): the live source drops ``blob_ref`` and
    re-authors a plain path under the reviewed name. Nothing raises, so the
    sentinel is stamped over the re-authored path in active AND terminal sessions
    — a provider-visible false custody claim tracked by elspeth-c72a3d09e5 /
    elspeth-24bf6a047a. Narrowing it here would alter a non-raising projection
    and drift stored guided_response_hash values, so this pins the current
    behaviour deliberately."""
    sentinel = "blob:360e1583-ae3c-4135-9240-0a26a14cf22f"
    sources = {"source": {"plugin": "csv", "options": {"path": "data.csv"}}}
    composer_meta = {
        "guided_session": {
            "reviewed_sources": {
                "11111111-1111-4111-8111-111111111111": {"name": "source", "plugin": "csv", "options": {"path": sentinel}}
            },
            "pending_source_intents": {},
            "terminal": terminal,
        }
    }

    sources_out, meta_out = _project(sources, composer_meta)

    assert sources_out["source"]["options"]["path"] == sentinel
    assert "custody_unavailable" not in meta_out["guided_session"]


def test_redact_guided_snapshot_rejects_malformed_terminal_before_degrading() -> None:
    from elspeth.web.composer.guided.errors import InvariantError

    sources, composer_meta = _two_guided_committed_sources_repointed_after_exit({"kind": "exited_to_freeform"})
    with pytest.raises(InvariantError, match=r"TerminalState\.from_dict"):
        _project(sources, composer_meta)


def _incident_active_shape() -> tuple[dict[str, Any], dict[str, Any]]:
    private = "/srv/elspeth/data/blobs/s1/50f5b3e9-f52f-4c5f-98df-a20ec7b2627b_colours.csv"
    sources = {"source": {"plugin": "csv", "options": {"path": private, "blob_ref": "50f5b3e9-f52f-4c5f-98df-a20ec7b2627b"}}}
    composer_meta = {
        "guided_session": {
            "reviewed_sources": {
                "11111111-1111-4111-8111-111111111111": {
                    "name": "source",
                    "plugin": "csv",
                    "options": {"path": "blob:360e1583-ae3c-4135-9240-0a26a14cf22f"},
                }
            },
            "pending_source_intents": {},
            "terminal": None,
        }
    }
    return sources, composer_meta


def test_assert_guided_custody_persistable_passes_without_a_guided_snapshot() -> None:
    from elspeth.web.composer.redaction import assert_guided_custody_persistable

    sources, _meta = _incident_active_shape()
    assert_guided_custody_persistable(sources, None)
    assert_guided_custody_persistable(sources, {"repair_turns_used": 0})
    assert_guided_custody_persistable(None, None)


def test_assert_guided_custody_persistable_rejects_an_active_unbindable_pair() -> None:
    from elspeth.web.composer.redaction import assert_guided_custody_persistable

    sources, composer_meta = _incident_active_shape()
    with pytest.raises(AuditIntegrityError, match="guided blob"):
        assert_guided_custody_persistable(sources, composer_meta)


@_TERMINALS
def test_assert_guided_custody_persistable_passes_terminal_pairs_that_project_degraded(terminal: dict[str, Any]) -> None:
    from elspeth.web.composer.redaction import assert_guided_custody_persistable

    sources, composer_meta = _incident_active_shape()
    composer_meta["guided_session"]["terminal"] = terminal
    assert_guided_custody_persistable(sources, composer_meta)
    assert _project(sources, composer_meta)[1]["guided_session"]["custody_unavailable"] is True


def test_assert_guided_custody_persistable_agrees_with_projection_on_the_fork_shape() -> None:
    """Gate and projection consume the same raw inputs, so the fork-rehydrated
    explicit-blob_ref shape that the projection accepts is also persistable, and
    the shape the projection rejects in an active session is not."""
    from elspeth.web.composer.redaction import assert_guided_custody_persistable

    raw_sources, composer_meta = _fork_explicit_shape()
    composer_meta["guided_session"]["terminal"] = None
    assert_guided_custody_persistable(raw_sources, composer_meta)
    assert _project(raw_sources, composer_meta)[0]["source"]["options"]["path"] == REDACTED_BLOB_SOURCE_PATH

    renamed = {"renamed": raw_sources["source"]}
    with pytest.raises(AuditIntegrityError):
        assert_guided_custody_persistable(renamed, composer_meta)
    with pytest.raises(AuditIntegrityError):
        _project(renamed, composer_meta)


def test_redact_guided_snapshot_non_custody_integrity_raise_escapes_the_terminal_branch() -> None:
    """The terminal branch degrades CUSTODY failures only (fix round 1 F-A3): a
    projected/raw shape disagreement is a programming error and must surface."""
    private = "/srv/elspeth/data/blobs/s1/shape.csv"
    raw_sources = {"source": {"plugin": "csv", "options": {"path": private, "blob_ref": "50f5b3e9-f52f-4c5f-98df-a20ec7b2627b"}}}
    projected = {"source": {"plugin": "csv"}}
    composer_meta = {
        "guided_session": {
            "reviewed_sources": {
                "11111111-1111-4111-8111-111111111111": {
                    "name": "source",
                    "plugin": "csv",
                    "options": {"path": private, "blob_ref": "50f5b3e9-f52f-4c5f-98df-a20ec7b2627b"},
                }
            },
            "pending_source_intents": {},
            "terminal": _EXITED_TERMINAL,
        }
    }
    with pytest.raises(AuditIntegrityError, match="mirror the raw source shape") as excinfo:
        redact_guided_snapshot_storage_paths(projected, composer_meta, raw_sources=raw_sources)
    assert not isinstance(excinfo.value, GuidedCustodyIntegrityError)


@pytest.mark.parametrize("terminal", [None, _EXITED_TERMINAL], ids=["active", "exited"])
def test_redact_guided_snapshot_malformed_implicit_decisions_is_not_a_custody_condition(terminal: object) -> None:
    """A malformed implicit_decisions report is a serializer defect, not a
    custody condition (fix round 1 F-A3): it must raise plain
    AuditIntegrityError on active AND terminal tips — never the custody type
    the 409 arms name, never the degraded projection."""
    real_path = "/internal/blobs/session/source.csv"
    sources = {"source": {"options": {"path": real_path, "schema": {"mode": "observed"}}}}
    composer_meta = {
        "guided_session": {
            "reviewed_sources": {
                "22222222-2222-4222-8222-222222222222": {"name": "source", "options": {"path": "blob:11111111-1111-4111-8111-111111111111"}}
            },
            "pending_source_intents": {},
            "terminal": terminal,
        },
        "implicit_decisions": {"schema_version": 1, "entries": "not-a-list"},
    }
    with pytest.raises(AuditIntegrityError, match="implicit-decision projection is malformed") as excinfo:
        _project(sources, composer_meta)
    assert not isinstance(excinfo.value, GuidedCustodyIntegrityError)


def test_redact_guided_snapshot_degrade_value_sweeps_planted_private_paths() -> None:
    """Degrade branch only (fix round 1 F-B1): after key-masking, any string in
    the projected sources or composer_meta EQUAL to a raw live carrier value is
    masked too, so a private path planted under a non-carrier key (options.glob,
    an implicit_decisions entry labeled outside source.path/file) cannot ride
    out on the degraded projection. The branch is new at this fix, so the sweep
    carries no stored-hash risk."""
    sources, composer_meta = _two_guided_committed_sources_repointed_after_exit(_EXITED_TERMINAL)
    sources["a"]["options"]["glob"] = _PRIVATE_A
    composer_meta["implicit_decisions"]["entries"].append({"path": "source.nested.path", "value": _PRIVATE_A, "category": "source"})

    sources_out, meta_out = _project(sources, composer_meta)

    projected = json.dumps((sources_out, meta_out))
    assert _PRIVATE_A not in projected
    assert sources_out["a"]["options"]["glob"] == REDACTED_BLOB_SOURCE_PATH
    assert meta_out["guided_session"]["custody_unavailable"] is True
