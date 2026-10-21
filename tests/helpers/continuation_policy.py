"""Complete admitted policy fixture shared by continuation owner tests."""

from elspeth.web.composer.continuation_policy import (
    ContinuationActionBounds,
    ContinuationActionPolicy,
    ContinuationAdmissionPolicy,
    ContinuationSegmentPolicy,
)


def minimal_continuation_policy() -> ContinuationAdmissionPolicy:
    provider = ContinuationActionPolicy(preparation=1.0, guard=5.0, quota=5.0, physical=120.0, retention=5.0)
    local = ContinuationActionPolicy(preparation=1.0, guard=None, quota=None, physical=5.0, retention=1.0)
    return ContinuationAdmissionPolicy(
        segment=ContinuationSegmentPolicy(nominal=300.0, watermark=120.0, quantum=180.0, reserve=25.0),
        actions=ContinuationActionBounds(
            primary=provider, advisor=provider, title=provider, read=local, session_sql=local, blob=local, external=local
        ),
    )
