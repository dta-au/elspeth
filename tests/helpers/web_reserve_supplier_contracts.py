"""Concrete Context and execution-obligation producer contracts, stdlib AST."""

import ast
import builtins

CONTEXT = "src/elspeth/contracts/session_operation.py"
REEXPORT = "src/elspeth/web/coordination/contracts.py"
EXECUTION = "src/elspeth/web/execution_lease_cleanup.py"


def contracts(h):
    n, c, a, s, call = (h[name] for name in ("n", "c", "a", "s", "call"))
    cmp, either, both, neg = (h[name] for name in ("cmp", "either", "both", "neg"))
    assign, annotate, expr, ret, raising, iff, with_ = (
        h[name] for name in ("assign", "annotate", "expr", "ret", "raising", "iff", "with_")
    )
    index, union, seq, list_, try_, handler = (h[name] for name in ("index", "union", "seq", "list_", "try_", "handler"))
    error = n("AuditIntegrityError")
    own_registry = a(s("registry"), "_lock")
    require_self = expr(call(a(s("registry"), "_require_obligation"), n("self")))

    context = {
        "SessionOperationContext.__post_init__": (
            ("self",),
            (),
            (),
            (),
            False,
            (),
            [
                iff(cmp(call("type", s("fence")), ast.IsNot, n("SessionOperationFence")), [raising(n("TypeError"))]),
                iff(cmp(call("type", s("operation_kind")), ast.IsNot, n("SessionOperationKind")), [raising(n("TypeError"))]),
            ],
        ),
        "SessionOperationFence.__post_init__": (
            ("self",),
            (),
            (),
            (),
            False,
            (),
            [
                *(
                    expr(call("_require_nonblank", s(field), c("SessionOperationFence." + field)))
                    for field in ("session_id", "operation_id", "lease_token")
                ),
                expr(call("_require_positive_int", s("operation_epoch"), c("SessionOperationFence.operation_epoch"))),
            ],
        ),
        "_require_nonblank": (
            ("value", "field_name"),
            (),
            (),
            (),
            False,
            (),
            [
                iff(
                    either(cmp(call("type", n("value")), ast.IsNot, n("str")), neg(call(a(n("value"), "strip")))),
                    [
                        ast.Raise(
                            exc=call(
                                "ValueError",
                                ast.JoinedStr(
                                    values=[
                                        ast.FormattedValue(value=n("field_name"), conversion=-1, format_spec=None),
                                        c(" must be a nonblank exact string"),
                                    ]
                                ),
                            ),
                            cause=None,
                        ),
                    ],
                ),
            ],
        ),
        "_require_positive_int": (
            ("value", "field_name"),
            (),
            (),
            (),
            False,
            (),
            [
                iff(
                    either(cmp(call("type", n("value")), ast.IsNot, n("int")), cmp(n("value"), ast.Lt, c(1))),
                    [
                        ast.Raise(
                            exc=call(
                                "ValueError",
                                ast.JoinedStr(
                                    values=[
                                        ast.FormattedValue(value=n("field_name"), conversion=-1, format_spec=None),
                                        c(" must be a positive exact integer"),
                                    ]
                                ),
                            ),
                            cause=None,
                        ),
                    ],
                ),
            ],
        ),
    }
    plain_values = ("registry", "authority", "session_id", "owner_instance_id", "lease_seconds", "executor", "generation")
    annotated_none = {
        "_registration": union(n("_ExecutionRegistration"), c(None)),
        "_retirement": union(n("_ExecutionRetirement"), c(None)),
        "release_submission": union(n("ExecutionLeaseSQLSubmission"), c(None)),
        "context": union(n("SessionOperationContext"), c(None)),
        "lease": union(n("SessionOperationLease"), c(None)),
        "construction_error": union(n("BaseException"), c(None)),
        "renewal_task": union(index(a(n("asyncio"), "Task"), c(None)), c(None)),
        "completion": union(index(n("Future"), c(None)), c(None)),
        "completion_task": union(index(a(n("asyncio"), "Task"), c(None)), c(None)),
        "completion_original_error": union(n("BaseException"), c(None)),
        "pipeline": union(index(n("Future"), union(index(n("Literal"), c("graceful_shutdown_handled")), c(None))), c(None)),
        "lifecycle_task": union(index(a(n("asyncio"), "Task"), c(None)), c(None)),
        "lifecycle_original_error": union(n("BaseException"), c(None)),
        "pipeline_submission_unknown": union(n("BaseException"), c(None)),
    }
    field_order = (
        "registry",
        "_registration",
        "_retirement",
        "authority",
        "session_id",
        "owner_instance_id",
        "lease_seconds",
        "executor",
        "generation",
        "_acquire",
        "_release",
        "acquire_submission",
        "release_submission",
        "context",
        "lease",
        "construction_error",
        "renewal_task",
        "renewal_allocation_declared",
        "release_succeeded",
        "no_resource",
        "retired",
        "completion",
        "completion_task",
        "completion_required",
        "completion_observed",
        "completion_outcome_recorded",
        "completion_original_error",
        "pipeline",
        "lifecycle_task",
        "lifecycle_required",
        "lifecycle_observed",
        "lifecycle_outcome_recorded",
        "lifecycle_original_error",
        "pipeline_submission_unknown",
        "unknown_pipeline_cleanup_declared",
        "observation_failures",
    )
    obligation_body = [iff(cmp(n("seal"), ast.IsNot, n("_ISSUANCE_SEAL")), [raising(error)])]
    for field in field_order:
        if field in plain_values:
            obligation_body.append(assign(s(field), n(field)))
        elif field in annotated_none:
            obligation_body.append(annotate(s(field), annotated_none[field], c(None)))
        elif field in ("_acquire", "_release"):
            obligation_body.append(assign(s(field), a(n("authority"), field[1:])))
        elif field == "acquire_submission":
            obligation_body.append(
                assign(s(field), call("ExecutionLeaseSQLSubmission", n("_ISSUANCE_SEAL"), n("self"), a(n("_ExecutionSQLArm"), "ACQUIRE")))
            )
        elif field == "observation_failures":
            obligation_body.append(
                annotate(s(field), index(n("dict"), seq(n("_ExecutionObservationPhase"), n("BaseException"))), ast.Dict(keys=[], values=[]))
            )
        else:
            obligation_body.append(assign(s(field), c(False)))
    execution = {
        "ExecutionAcquisitionObligation.__init__": (
            ("self", "seal", "registry", "authority", "session_id", "owner_instance_id", "lease_seconds", "executor", "generation"),
            (),
            (),
            (),
            False,
            (),
            obligation_body,
        ),
        "ExecutionAcquisitionObligation.validate_acquisition_request": (
            ("self", "authority"),
            ("session_id", "owner_instance_id", "lease_seconds"),
            (),
            (None, None, None),
            False,
            (),
            [
                with_(
                    own_registry,
                    [
                        require_self,
                        iff(
                            either(
                                cmp(n("authority"), ast.IsNot, s("authority")),
                                *(cmp(n(field), ast.NotEq, s(field)) for field in ("session_id", "owner_instance_id", "lease_seconds")),
                            ),
                            [raising(error)],
                        ),
                    ],
                ),
            ],
        ),
        "ExecutionAcquisitionObligation.retain_lease_construction": (
            ("self", "lease"),
            (),
            (),
            (),
            False,
            (),
            [
                ast.ImportFrom(
                    module="elspeth.web.coordination.lifecycle", names=[ast.alias(name="SessionOperationLease", asname=None)], level=0
                ),
                with_(
                    own_registry,
                    [
                        require_self,
                        iff(
                            either(
                                cmp(call("type", n("lease")), ast.IsNot, n("SessionOperationLease")),
                                cmp(s("lease"), ast.IsNot, c(None)),
                                cmp(s("context"), ast.Is, c(None)),
                            ),
                            [raising(error)],
                        ),
                        assign(s("lease"), n("lease")),
                    ],
                ),
            ],
        ),
        "ExecutionAcquisitionObligation.declare_renewal_allocation": (
            ("self", "lease"),
            (),
            (),
            (),
            False,
            (),
            [
                with_(
                    own_registry,
                    [
                        require_self,
                        iff(either(cmp(n("lease"), ast.IsNot, s("lease")), s("renewal_allocation_declared")), [raising(error)]),
                        assign(s("renewal_allocation_declared"), c(True)),
                    ],
                ),
            ],
        ),
        "ExecutionAcquisitionObligation.bind_actual_renewal_task": (
            ("self", "lease", "task"),
            (),
            (),
            (),
            False,
            (),
            [
                with_(
                    own_registry,
                    [
                        require_self,
                        iff(
                            either(
                                neg(call("isinstance", n("task"), a(n("asyncio"), "Task"))),
                                cmp(n("lease"), ast.IsNot, s("lease")),
                                neg(s("renewal_allocation_declared")),
                                both(cmp(s("renewal_task"), ast.IsNot, c(None)), cmp(s("renewal_task"), ast.IsNot, n("task"))),
                            ),
                            [raising(error)],
                        ),
                        assign(s("renewal_task"), n("task")),
                    ],
                ),
            ],
        ),
        "ExecutionLeaseSQLSubmission.__init__": (
            ("self", "seal", "obligation", "arm"),
            (),
            (),
            (),
            False,
            (),
            [
                iff(
                    either(
                        cmp(n("seal"), ast.IsNot, n("_ISSUANCE_SEAL")),
                        cmp(call("type", n("obligation")), ast.IsNot, n("ExecutionAcquisitionObligation")),
                        cmp(call("type", n("arm")), ast.IsNot, n("_ExecutionSQLArm")),
                    ),
                    [raising(error)],
                ),
                assign(s("obligation"), n("obligation")),
                assign(s("arm"), n("arm")),
                annotate(s("future"), union(index(n("Future"), union(n("SessionOperationContext"), c(None))), c(None)), c(None)),
                annotate(s("reservation"), union(n("InvocationReservation"), c(None)), c(None)),
                annotate(s("original_error"), union(n("BaseException"), c(None)), c(None)),
                annotate(s("source_value"), union(n("SessionOperationContext"), c(None)), c(None)),
                annotate(s("domain_refusal"), union(n("BaseException"), c(None)), c(None)),
                assign(s("observed"), c(False)),
                assign(s("no_submission"), c(False)),
                assign(s("callback_return_observed"), c(False)),
                annotate(s("projection_failure"), union(n("BaseException"), c(None)), c(None)),
            ],
        ),
        "ExecutionLeaseReleaseRegistry.__init__": (
            ("self",),
            ("owner", "recovery", "loop"),
            (),
            (None, None, None),
            False,
            (),
            [
                ast.ImportFrom(module="elspeth.web.process_recovery", names=[ast.alias(name="ProcessRecovery", asname=None)], level=0),
                iff(
                    either(
                        cmp(call("type", n("owner")), ast.IsNot, n("ApplicationFinalizerOwner")),
                        cmp(call("type", n("recovery")), ast.IsNot, n("ProcessRecovery")),
                    ),
                    [raising(error)],
                ),
                assign(s("owner"), n("owner")),
                annotate(s("_executor_finalizer"), union(n("ApplicationFinalizerCapability"), c(None)), c(None)),
                assign(s("_executor_allocation_declared"), c(False)),
                annotate(s("_execution_executor"), union(n("ThreadPoolExecutor"), c(None)), c(None)),
                assign(s("_executor_join_returned"), c(False)),
                assign(s("recovery"), n("recovery")),
                assign(s("loop"), n("loop")),
                assign(s("_lock"), call(a(n("threading"), "RLock"))),
                assign(s("_sealed"), c(False)),
                annotate(s("_pending"), index(n("dict"), seq(n("int"), n("ExecutionAcquisitionObligation"))), ast.Dict(keys=[], values=[])),
                annotate(s("_failures"), index(n("list"), n("BaseException")), list_()),
                assign(s("_recovery_requested"), c(False)),
            ],
        ),
        "ExecutionLeaseReleaseRegistry.admit": (
            ("self", "authority"),
            ("session_id", "owner_instance_id", "lease_seconds"),
            (),
            (None, None, None),
            False,
            (),
            [
                ast.ImportFrom(
                    module="elspeth.web.async_workers",
                    names=[
                        ast.alias(name=name, asname=None)
                        for name in ("_APPLICATION_FINALIZER_OWNER", "_INSTANCE_DRAINING", "_generation_for", "_get_shared_executor")
                    ],
                    level=0,
                ),
                ast.ImportFrom(
                    module="elspeth.web.coordination.repository",
                    names=[
                        ast.alias(name=name, asname=None)
                        for name in ("PostgresSessionOperationRepository", "_SessionOperationAuthorityRepository")
                    ],
                    level=0,
                ),
                ast.ImportFrom(
                    module="elspeth.web.coordination.sqlite_authority",
                    names=[ast.alias(name="SQLiteLocalSessionOperationAuthority", asname=None)],
                    level=0,
                ),
                iff(
                    either(
                        neg(call("isinstance", n("authority"), n("_SessionOperationAuthorityRepository"))),
                        cmp(
                            call("type", n("authority")),
                            ast.NotIn,
                            seq(n("SQLiteLocalSessionOperationAuthority"), n("PostgresSessionOperationRepository")),
                        ),
                        cmp(call("type", n("session_id")), ast.IsNot, n("UUID")),
                        cmp(call("type", n("owner_instance_id")), ast.IsNot, n("str")),
                        neg(call(a(n("owner_instance_id"), "strip"))),
                        cmp(call("type", n("lease_seconds")), ast.IsNot, n("int")),
                        cmp(n("lease_seconds"), ast.Lt, c(1)),
                    ),
                    [raising(error)],
                ),
                assign(seq(n("acquire"), n("release")), seq(a(n("authority"), "acquire"), a(n("authority"), "release"))),
                iff(
                    either(
                        *(
                            check
                            for method in ("acquire", "release")
                            for check in (
                                cmp(call("type", n(method)), ast.IsNot, n("MethodType")),
                                cmp(a(n(method), "__self__"), ast.IsNot, n("authority")),
                                cmp(a(n(method), "__func__"), ast.IsNot, a(n("_SessionOperationAuthorityRepository"), method)),
                            )
                        )
                    ),
                    [raising(error)],
                ),
                iff(
                    either(
                        cmp(n("_APPLICATION_FINALIZER_OWNER"), ast.IsNot, s("owner")),
                        cmp(n("_INSTANCE_DRAINING"), ast.IsNot, a(s("recovery"), "instance_draining")),
                    ),
                    [raising(error)],
                ),
                assign(n("executor"), call("_get_shared_executor")),
                assign(n("generation"), call("_generation_for", n("executor"))),
                iff(cmp(a(n("generation"), "instance_draining"), ast.IsNot, a(s("recovery"), "instance_draining")), [raising(error)]),
                with_(
                    s("_lock"),
                    [
                        iff(
                            either(s("_sealed"), call(a(a(s("recovery"), "instance_draining"), "is_set"))),
                            [raising(n("RequiredGenerationUnavailable"))],
                        ),
                        assign(
                            n("obligation"),
                            call(
                                "ExecutionAcquisitionObligation",
                                n("_ISSUANCE_SEAL"),
                                n("self"),
                                n("authority"),
                                n("session_id"),
                                n("owner_instance_id"),
                                n("lease_seconds"),
                                n("executor"),
                                n("generation"),
                            ),
                        ),
                        assign(a(n("obligation"), "_registration"), call("_ExecutionRegistration", n("self"), n("obligation"))),
                        assign(index(s("_pending"), call("id", n("obligation"))), n("obligation")),
                        ret(n("obligation")),
                    ],
                ),
            ],
        ),
        "ExecutionLeaseReleaseRegistry._require_obligation": (
            ("self", "obligation"),
            (),
            (),
            (),
            False,
            (),
            [
                iff(
                    either(
                        cmp(call("type", n("obligation")), ast.IsNot, n("ExecutionAcquisitionObligation")),
                        cmp(a(n("obligation"), "registry"), ast.IsNot, n("self")),
                    ),
                    [raising(error)],
                ),
                assign(n("registration"), a(n("obligation"), "_registration")),
                iff(
                    either(
                        cmp(n("registration"), ast.Is, c(None)),
                        cmp(a(n("registration"), "registry"), ast.IsNot, n("self")),
                        cmp(a(n("registration"), "obligation"), ast.IsNot, n("obligation")),
                    ),
                    [raising(error)],
                ),
                assign(n("retained"), call(a(s("_pending"), "get"), call("id", n("obligation")))),
                assign(n("retirement"), a(n("obligation"), "_retirement")),
                iff(cmp(n("retained"), ast.Is, n("obligation")), [ret()]),
                iff(
                    either(
                        cmp(n("retained"), ast.IsNot, c(None)),
                        cmp(n("retirement"), ast.Is, c(None)),
                        cmp(a(n("retirement"), "registration"), ast.IsNot, n("registration")),
                        cmp(a(a(n("retirement"), "submission"), "obligation"), ast.IsNot, n("obligation")),
                    ),
                    [raising(error)],
                ),
            ],
        ),
        "ExecutionLeaseReleaseRegistry._require_submission": (
            ("self", "submission"),
            (),
            (),
            (),
            False,
            (),
            [
                iff(
                    either(
                        cmp(call("type", n("submission")), ast.IsNot, n("ExecutionLeaseSQLSubmission")),
                        cmp(call("type", a(n("submission"), "arm")), ast.IsNot, n("_ExecutionSQLArm")),
                    ),
                    [raising(error)],
                ),
                assign(n("obligation"), a(n("submission"), "obligation")),
                expr(call(s("_require_obligation"), n("obligation"))),
                assign(
                    n("expected"),
                    ast.IfExp(
                        test=cmp(a(n("submission"), "arm"), ast.Is, a(n("_ExecutionSQLArm"), "ACQUIRE")),
                        body=a(n("obligation"), "acquire_submission"),
                        orelse=a(n("obligation"), "release_submission"),
                    ),
                ),
                iff(cmp(n("submission"), ast.IsNot, n("expected")), [raising(error)]),
            ],
        ),
    }
    # Full sole context producer from a joined issued SQL invocation. No
    # physical completion fact is inferred merely from these source checks.
    execution["ExecutionLeaseSQLSubmission.observe_future"] = (
        ("self", "actual"),
        (),
        (),
        (),
        False,
        (),
        [
            assign(n("registry"), a(s("obligation"), "registry")),
            annotate(n("failed_cleanup"), union(n("BaseException"), c(None)), c(None)),
            iff(cmp(s("projection_failure"), ast.IsNot, c(None)), [ret()]),
            try_(
                [
                    with_(
                        a(n("registry"), "_lock"),
                        [
                            expr(call(a(n("registry"), "_require_submission"), n("self"))),
                            iff(
                                either(
                                    cmp(n("actual"), ast.IsNot, s("future")),
                                    neg(call(a(n("actual"), "done"))),
                                    cmp(s("reservation"), ast.Is, c(None)),
                                ),
                                [raising(error)],
                            ),
                            iff(s("observed"), [ret()]),
                            iff(either(neg(a(s("reservation"), "released")), neg(s("callback_return_observed"))), [ret()]),
                            assign(n("trace"), call(a(a(s("reservation"), "witness"), "snapshot"))),
                            iff(
                                both(
                                    a(n("trace"), "valid_aborted_exit"),
                                    either(
                                        cmp(a(s("reservation"), "setup_error"), ast.IsNot, c(None)),
                                        cmp(a(s("reservation"), "submission_error"), ast.IsNot, c(None)),
                                    ),
                                ),
                                [ret()],
                            ),
                            iff(
                                either(a(n("trace"), "impossible"), neg(a(n("trace"), "exited")), neg(a(n("trace"), "callable_finished"))),
                                [raising(error)],
                            ),
                            try_(
                                [assign(n("value"), call(a(n("actual"), "result")))],
                                [
                                    handler(
                                        n("BaseException"),
                                        "original",
                                        [
                                            assign(s("original_error"), n("original")),
                                            assign(s("observed"), c(True)),
                                            iff(
                                                both(
                                                    cmp(s("arm"), ast.Is, a(n("_ExecutionSQLArm"), "ACQUIRE")),
                                                    cmp(n("original"), ast.Is, s("domain_refusal")),
                                                ),
                                                [expr(call(a(n("registry"), "_retire_no_resource"), s("obligation")))],
                                                [
                                                    iff(
                                                        both(
                                                            cmp(s("arm"), ast.Is, a(n("_ExecutionSQLArm"), "RELEASE")),
                                                            a(s("obligation"), "release_lost"),
                                                        ),
                                                        [expr(call(a(n("registry"), "_retire_settled_if_complete"), s("obligation")))],
                                                        [
                                                            expr(call(a(n("registry"), "_retain_failure"), n("original"))),
                                                            assign(n("failed_cleanup"), n("original")),
                                                        ],
                                                    )
                                                ],
                                            ),
                                        ],
                                    )
                                ],
                                [
                                    iff(
                                        cmp(s("arm"), ast.Is, a(n("_ExecutionSQLArm"), "ACQUIRE")),
                                        [
                                            iff(
                                                either(
                                                    cmp(call("type", n("value")), ast.IsNot, n("SessionOperationContext")),
                                                    cmp(n("value"), ast.IsNot, s("source_value")),
                                                    cmp(
                                                        a(n("value"), "operation_kind"), ast.IsNot, a(n("SessionOperationKind"), "EXECUTE")
                                                    ),
                                                    cmp(
                                                        a(a(n("value"), "fence"), "session_id"),
                                                        ast.NotEq,
                                                        call("str", a(s("obligation"), "session_id")),
                                                    ),
                                                ),
                                                [raising(error)],
                                            ),
                                            assign(a(s("obligation"), "context"), n("value")),
                                        ],
                                        [iff(cmp(n("value"), ast.IsNot, c(None)), [raising(error)])],
                                    ),
                                    assign(s("observed"), c(True)),
                                    iff(
                                        cmp(s("arm"), ast.Is, a(n("_ExecutionSQLArm"), "RELEASE")),
                                        [
                                            assign(a(s("obligation"), "release_succeeded"), c(True)),
                                            expr(call(a(n("registry"), "_retire_settled_if_complete"), s("obligation"))),
                                        ],
                                    ),
                                ],
                            ),
                        ],
                    ),
                    iff(
                        cmp(n("failed_cleanup"), ast.IsNot, c(None)), [expr(call(a(n("registry"), "record_failure"), n("failed_cleanup")))]
                    ),
                ],
                [
                    handler(
                        n("BaseException"),
                        "original",
                        [assign(s("projection_failure"), n("original")), expr(call(a(n("registry"), "record_failure"), n("original")))],
                    )
                ],
            ),
        ],
    )
    dispatch_guard = either(
        *(
            check
            for method in ("acquire", "release")
            for check in (
                cmp(call("type", n(method)), ast.IsNot, n("MethodType")),
                cmp(a(n(method), "__self__"), ast.IsNot, a(n("obligation"), "authority")),
                cmp(a(n(method), "__func__"), ast.IsNot, a(n("_SessionOperationAuthorityRepository"), method)),
            )
        )
    )
    captured_dispatch = assign(seq(n("acquire"), n("release")), seq(a(n("obligation"), "_acquire"), a(n("obligation"), "_release")))
    execution["ExecutionLeaseSQLSubmission.assert_canonical_dispatch"] = (
        ("self",),
        (),
        (),
        (),
        False,
        (),
        [
            ast.ImportFrom(
                module="elspeth.web.coordination.repository",
                names=[ast.alias(name="_SessionOperationAuthorityRepository", asname=None)],
                level=0,
            ),
            assign(n("obligation"), s("obligation")),
            with_(
                a(a(n("obligation"), "registry"), "_lock"),
                [expr(call(a(a(n("obligation"), "registry"), "_require_submission"), n("self")))],
            ),
            captured_dispatch,
            iff(dispatch_guard, [raising(error)]),
            iff(
                both(
                    cmp(s("arm"), ast.Is, a(n("_ExecutionSQLArm"), "RELEASE")),
                    either(
                        cmp(call("type", a(n("obligation"), "context")), ast.IsNot, n("SessionOperationContext")),
                        cmp(a(n("obligation"), "context"), ast.IsNot, a(a(n("obligation"), "acquire_submission"), "source_value")),
                        neg(a(a(n("obligation"), "acquire_submission"), "observed")),
                    ),
                ),
                [raising(error)],
            ),
        ],
    )
    execution["ExecutionLeaseSQLSubmission.invoke"] = (
        ("self",),
        (),
        (),
        (),
        False,
        (),
        [
            ast.ImportFrom(
                module="elspeth.web.coordination.contracts", names=[ast.alias(name="SessionOperationFenceLost", asname=None)], level=0
            ),
            ast.ImportFrom(
                module="elspeth.web.coordination.repository",
                names=[
                    ast.alias(name=name, asname=None)
                    for name in ("CanonicalExecutionReleaseLoss", "SessionOperationConflictError", "_SessionOperationAuthorityRepository")
                ],
                level=0,
            ),
            assign(n("obligation"), s("obligation")),
            captured_dispatch,
            iff(dispatch_guard, [raising(error)]),
            iff(
                cmp(s("arm"), ast.Is, a(n("_ExecutionSQLArm"), "ACQUIRE")),
                [
                    try_(
                        [
                            annotate(
                                n("value"),
                                n("SessionOperationContext"),
                                call(
                                    "acquire",
                                    session_id=a(n("obligation"), "session_id"),
                                    operation_kind=a(n("SessionOperationKind"), "EXECUTE"),
                                    owner_instance_id=a(n("obligation"), "owner_instance_id"),
                                    lease_seconds=a(n("obligation"), "lease_seconds"),
                                ),
                            )
                        ],
                        [
                            handler(
                                seq(n("SessionOperationConflictError"), n("SessionOperationFenceLost")),
                                "original",
                                [assign(s("domain_refusal"), n("original")), ast.Raise(exc=None, cause=None)],
                            )
                        ],
                    ),
                    assign(s("source_value"), n("value")),
                    ret(n("value")),
                ],
            ),
            assign(n("context"), a(n("obligation"), "context")),
            iff(
                either(
                    cmp(call("type", n("context")), ast.IsNot, n("SessionOperationContext")),
                    cmp(n("context"), ast.IsNot, a(a(n("obligation"), "acquire_submission"), "source_value")),
                    neg(a(a(n("obligation"), "acquire_submission"), "observed")),
                    cmp(a(n("context"), "operation_kind"), ast.IsNot, a(n("SessionOperationKind"), "EXECUTE")),
                    cmp(a(a(n("context"), "fence"), "session_id"), ast.NotEq, call("str", a(n("obligation"), "session_id"))),
                ),
                [raising(error)],
            ),
            try_(
                [expr(call("release", n("context")))],
                [
                    handler(
                        n("CanonicalExecutionReleaseLoss"),
                        "original",
                        [
                            iff(
                                both(
                                    cmp(call("type", n("original")), ast.Is, n("CanonicalExecutionReleaseLoss")),
                                    cmp(a(n("original"), "context"), ast.Is, n("context")),
                                ),
                                [assign(s("domain_refusal"), n("original"))],
                            ),
                            ast.Raise(exc=None, cause=None),
                        ],
                    )
                ],
            ),
            ret(c(None)),
        ],
    )
    execution["ExecutionLeaseSQLSubmission.bind_reservation"] = (
        ("self", "reservation"),
        (),
        (),
        (),
        False,
        (),
        [
            with_(
                a(a(s("obligation"), "registry"), "_lock"),
                [
                    expr(call(a(a(s("obligation"), "registry"), "_require_submission"), n("self"))),
                    iff(
                        either(
                            cmp(s("reservation"), ast.IsNot, c(None)),
                            cmp(a(n("reservation"), "_registering_generation"), ast.IsNot, a(s("obligation"), "generation")),
                        ),
                        [raising(error)],
                    ),
                    assign(s("reservation"), n("reservation")),
                ],
            ),
        ],
    )
    execution["ExecutionLeaseSQLSubmission.bind_future"] = (
        ("self", "future", "reservation"),
        (),
        (),
        (),
        False,
        (),
        [
            assign(n("registry"), a(s("obligation"), "registry")),
            with_(
                a(n("registry"), "_lock"),
                [
                    expr(call(a(n("registry"), "_require_submission"), n("self"))),
                    iff(either(cmp(s("future"), ast.IsNot, c(None)), cmp(s("reservation"), ast.IsNot, n("reservation"))), [raising(error)]),
                    iff(
                        either(
                            cmp(a(n("reservation"), "future"), ast.IsNot, n("future")),
                            cmp(a(n("reservation"), "_registering_generation"), ast.IsNot, a(s("obligation"), "generation")),
                        ),
                        [raising(error)],
                    ),
                    assign(s("future"), n("future")),
                    assign(s("reservation"), n("reservation")),
                ],
            ),
        ],
    )
    return {CONTEXT: context, EXECUTION: execution}


def _dataclass(unit, h, name, fields):
    cls = h["_find"](unit.tree, name)
    if not isinstance(cls, ast.ClassDef) or cls.bases or cls.keywords or cls.type_params:
        return [f"{name}: owned dataclass construction changed"]
    expected_decorators = [h["n"]("final"), h["call"]("dataclass", frozen=h["c"](True), slots=h["c"](True))]
    declarations = [
        node
        for node in cls.body
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant))
    ]
    methods = [node.name for node in cls.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    if not h["_same"](cls.decorator_list, expected_decorators) or methods != ["__post_init__"]:
        return [f"{name}: generated constructor/descriptor flags or namespace changed"]
    if not h["_body_same"](declarations, [h["annotate"](h["n"](field), h["n"](annotation), None) for field, annotation in fields]):
        return [f"{name}: exact field constructor producer changed"]
    return []


def _reexports(unit, h):
    required = {"SessionOperationContext", "SessionOperationFence", "SessionOperationKind"}
    seen, failures = set(), []
    # Star imports do not name the namespace members they can overwrite.
    # This concrete supplier does not support them as reexport producers.
    if any(isinstance(node, ast.ImportFrom) and any(alias.name == "*" for alias in node.names) for node in ast.walk(unit.tree)):
        failures.append("Context reexport: unsupported star import namespace producer")
    # Class suites execute during their enclosing definition. A Global in
    # that suite redirects stores/deletes to this module, while an ordinary
    # same-spelled class field stays in the class namespace. Deferred method
    # bodies are deliberately excluded by the execution-scope visitor.
    for cls in (node for node in ast.walk(unit.tree) if isinstance(node, ast.ClassDef)):
        scope = h["_ExecutionScope"](postponed_annotations=h["_postponed_annotations"](unit))
        for statement in cls.body:
            scope.visit(statement)
        if scope.redirected & required:
            failures.append("Context reexport: class suite redirects protected module binding")
    for node in unit.tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "elspeth.contracts.session_operation":
            if node.level or any(alias.name not in required or alias.asname != alias.name for alias in node.names):
                failures.append("Context reexport: exact canonical import binding changed")
            for alias in node.names:
                if alias.name in seen:
                    failures.append("Context reexport: duplicated canonical binding")
                seen.add(alias.name)
        else:
            scope = h["_ExecutionScope"](postponed_annotations=h["_postponed_annotations"](unit))
            scope.visit(node)
            if (scope.bindings | scope.redirected) & required:
                failures.append("Context reexport: canonical binding has another lexical producer")
    if seen != required:
        failures.append("Context reexport: required exact canonical producers missing")
    return failures


def _direct_field_stores(tree):
    """All syntactic direct attribute writes/deletes, with lexical ownership.

    This is deliberately a whole-module syntactic inventory. It does not
    resolve receiver aliases or infer absence of reflected/external effects.
    """
    stores = []

    class Inventory(ast.NodeVisitor):
        def __init__(self):
            self.scope = ()

        def definition(self, node):
            previous = self.scope
            self.scope += (node.name,)
            self.generic_visit(node)
            self.scope = previous

        visit_ClassDef = definition
        visit_FunctionDef = definition
        visit_AsyncFunctionDef = definition

        def visit_Attribute(self, node):
            if node.attr in {"context", "lease", "source_value"} and isinstance(node.ctx, (ast.Store, ast.Del)):
                stores.append((self.scope, node.attr, ast.dump(node.value), type(node.ctx).__name__))
            self.generic_visit(node)

    Inventory().visit(tree)
    return sorted(stores)


def supplier_local_contracts(units, h):
    """Local producer premises plus typed-source roots, without admission."""
    indexed = {unit.path: unit for unit in units}
    failures, roots = [], set()
    for path, selected in contracts(h).items():
        unit = indexed.get(path)
        if unit is None:
            failures.append(f"{path}: selected supplier unit missing")
            continue
        if not h["_postponed_annotations"](unit):
            failures.append(f"{path}: supported supplier annotation mode changed")
        found, dependencies = h["_check_contracts"](unit, selected)
        failures.extend(found)
        roots.update(dependencies)
    context = indexed.get(CONTEXT)
    if context is not None:
        failures.extend(
            _dataclass(
                context, h, "SessionOperationContext", (("fence", "SessionOperationFence"), ("operation_kind", "SessionOperationKind"))
            )
        )
        failures.extend(
            _dataclass(
                context,
                h,
                "SessionOperationFence",
                (("session_id", "str"), ("operation_id", "str"), ("lease_token", "str"), ("operation_epoch", "int")),
            )
        )
        cls = h["_find"](context.tree, "SessionOperationKind")
        values = ("CREATE", "COMPOSE", "PROPOSAL", "EXECUTE", "ARCHIVE", "PROGRESS", "BLOB_READ", "SESSION_FORK")
        if (
            not isinstance(cls, ast.ClassDef)
            or cls.keywords
            or cls.type_params
            or cls.decorator_list
            or not h["_same"](cls.bases, [h["n"]("StrEnum")])
            or not h["_body_same"](cls.body, [h["assign"](h["n"](name), h["c"](name.lower())) for name in values])
        ):
            failures.append("SessionOperationKind: exact enum member/class construction changed")
        roots.update(
            {
                "elspeth.contracts.session_operation.SessionOperationContext",
                "elspeth.contracts.session_operation.SessionOperationFence",
                "elspeth.contracts.session_operation.SessionOperationKind",
                "dataclasses.dataclass",
                "typing.final",
                "enum.StrEnum",
                "builtins.__build_class__",
            }
        )
        for owner, fields in (
            ("SessionOperationContext", ("fence", "operation_kind")),
            ("SessionOperationFence", ("session_id", "operation_id", "lease_token", "operation_epoch")),
        ):
            roots.update("elspeth.contracts.session_operation." + owner + "." + field for field in fields)
    reexport = indexed.get(REEXPORT)
    if reexport is None:
        failures.append("Context reexport unit missing")
    else:
        failures.extend(_reexports(reexport, h))
        roots.update(
            "elspeth.web.coordination.contracts." + name
            for name in ("SessionOperationContext", "SessionOperationFence", "SessionOperationKind")
        )
    execution = indexed.get(EXECUTION)
    if execution is not None:
        for name in ("ExecutionAcquisitionObligation", "ExecutionLeaseSQLSubmission", "ExecutionLeaseReleaseRegistry"):
            cls = h["_find"](execution.tree, name)
            if not isinstance(cls, ast.ClassDef) or cls.bases or cls.keywords or cls.type_params or cls.decorator_list:
                failures.append(f"{name}: plain owned constructor/lookup changed")
                continue
            methods = [node for node in cls.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
            declarations = [node for node in cls.body if node not in methods]
            if not h["_body_same"](declarations, []):
                failures.append(f"{name}: executable class namespace effect changed")
            if len({method.name for method in methods}) != len(methods):
                failures.append(f"{name}: duplicate canonical method binding")
            dangerous = {
                "__getattribute__",
                "__getattr__",
                "__setattr__",
                "__delattr__",
                "__new__",
                "__init_subclass__",
                "__slots__",
                "context",
                "lease",
                "registry",
                "property",
                "classmethod",
                "staticmethod",
                "contextmanager",
            }
            for method in methods:
                if (
                    method.name in dangerous
                    or method.type_params
                    or method.type_comment
                    or not h["_postponed_annotations"](execution)
                    or any(
                        not isinstance(default, ast.Constant)
                        for default in (*method.args.defaults, *(default for default in method.args.kw_defaults if default is not None))
                    )
                    or any(
                        not isinstance(decorator, ast.Name) or decorator.id not in {"property", "contextmanager"}
                        for decorator in method.decorator_list
                    )
                ):
                    failures.append(f"{name}.{method.name}: unsupported owned definition/lookup effect")
                roots.update(h["_definition_dependencies"](execution, method))
            roots.add("elspeth.web.execution_lease_cleanup." + name)
        # Data-field producers actually used by the lease constructor. Alias
        # escapes and other modules are the common receiver/effect obligation.
        stores = _direct_field_stores(execution.tree)
        expected = [
            ("ExecutionLeaseSQLSubmission", "__init__", "source_value", ast.dump(h["n"]("self"))),
            ("ExecutionLeaseSQLSubmission", "invoke", "source_value", ast.dump(h["n"]("self"))),
            ("ExecutionLeaseSQLSubmission", "observe_future", "context", ast.dump(h["a"](h["s"]("obligation"), "context").value)),
            ("ExecutionAcquisitionObligation", "__init__", "context", ast.dump(h["n"]("self"))),
            ("ExecutionAcquisitionObligation", "__init__", "lease", ast.dump(h["n"]("self"))),
            ("ExecutionAcquisitionObligation", "retain_lease_construction", "lease", ast.dump(h["n"]("self"))),
        ]
        expected = sorted(((owner, method), member, receiver, "Store") for owner, method, member, receiver in expected)
        if stores != expected:
            failures.append("execution obligation: complete module context/lease/source_value producer inventory changed")
        for name, fields in (
            ("_ExecutionRegistration", (("registry", "ExecutionLeaseReleaseRegistry"), ("obligation", "ExecutionAcquisitionObligation"))),
            ("_ExecutionRetirement", (("registration", "_ExecutionRegistration"), ("submission", "ExecutionLeaseSQLSubmission"))),
        ):
            cls = h["_find"](execution.tree, name)
            if (
                not isinstance(cls, ast.ClassDef)
                or cls.bases
                or cls.keywords
                or cls.type_params
                or not h["_same"](cls.decorator_list, [h["call"]("dataclass", frozen=h["c"](True), slots=h["c"](True))])
                or not h["_body_same"](cls.body, [h["annotate"](h["n"](field), h["n"](annotation), None) for field, annotation in fields])
            ):
                failures.append(f"{name}: registration/retirement constructor identity changed")
        cls = h["_find"](execution.tree, "_ExecutionSQLArm")
        if (
            not isinstance(cls, ast.ClassDef)
            or cls.keywords
            or cls.type_params
            or cls.decorator_list
            or not h["_same"](cls.bases, [h["n"]("Enum")])
            or not h["_body_same"](cls.body, [h["assign"](h["n"](name), h["c"](name.lower())) for name in ("ACQUIRE", "RELEASE")])
        ):
            failures.append("_ExecutionSQLArm: exact enum producer changed")
        seals = [
            node
            for node in execution.tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "_ISSUANCE_SEAL" for target in node.targets)
        ]
        if len(seals) != 1 or not h["_same"](seals[0], h["assign"](h["n"]("_ISSUANCE_SEAL"), h["call"]("object"))):
            failures.append("execution obligation: actual issuance seal producer changed")
        roots.update(
            {
                "elspeth.web.execution_lease_cleanup._ISSUANCE_SEAL",
                "elspeth.web.execution_lease_cleanup._ExecutionRegistration",
                "elspeth.web.execution_lease_cleanup._ExecutionRetirement",
                "elspeth.web.execution_lease_cleanup._ExecutionSQLArm",
                "builtins.__build_class__",
                "enum.Enum",
                "dataclasses.dataclass",
                "builtins.object",
            }
        )
        roots.update(
            "elspeth.web.coordination.repository._SessionOperationAuthorityRepository." + method for method in ("acquire", "release")
        )
        for owner, fields in (
            ("_ExecutionRegistration", ("registry", "obligation")),
            ("_ExecutionRetirement", ("registration", "submission")),
        ):
            roots.update("elspeth.web.execution_lease_cleanup." + owner + "." + field for field in fields)
    return failures, roots


def supplier_failures(units, h):
    """Standalone refusal until the actual common supplier cut is qualified."""
    failures, roots = supplier_local_contracts(units, h)
    failures.append(
        "UNKNOWN supplier composition: source-local Context/obligation producers require canonical import/class/constructor receivers and all external descriptor, callback, alias and field effects before admission"
    )
    return failures, roots


def supplier_owned_method_definitions(units, h):
    """Definition effects of the finite canonical supplier classes.

    Their ordinary namespace grammar is checked by supplier_local_contracts.
    This enumerates definitions; it does not certify the unselected bodies.
    """
    classes = {
        CONTEXT: ("SessionOperationContext", "SessionOperationFence"),
        EXECUTION: ("ExecutionAcquisitionObligation", "ExecutionLeaseSQLSubmission", "ExecutionLeaseReleaseRegistry"),
    }
    for unit in units:
        for name in classes.get(unit.path, ()):
            cls = h["_find"](unit.tree, name)
            if isinstance(cls, ast.ClassDef):
                for method in cls.body:
                    if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        yield unit, name + "." + method.name, method


def supplier_dependency_roles(units, h, classifier):
    """Actual source ownership/uses; imported and receiver origins stay premises."""
    failures, roots = supplier_failures(units, h)
    index = {}
    for unit in units:
        module = classifier["module_name"](unit.path)
        for node in unit.tree.body:
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                identity = module + "." + node.name
                index[identity] = {
                    "kind": "owned_class" if isinstance(node, ast.ClassDef) else "owned_function",
                    "path": unit.path,
                    "line": node.lineno,
                }
                if isinstance(node, ast.ClassDef):
                    for method in node.body:
                        if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            index[identity + "." + method.name] = {
                                "kind": "receiver_bound_method",
                                "owner_class": identity,
                                "member": method.name,
                                "path": unit.path,
                                "line": method.lineno,
                            }
                            if method.name == "__init__":
                                for child in ast.walk(method):
                                    if (
                                        isinstance(child, ast.Attribute)
                                        and isinstance(child.value, ast.Name)
                                        and child.value.id == "self"
                                        and isinstance(child.ctx, ast.Store)
                                    ):
                                        index[identity + "." + child.attr] = {
                                            "kind": "receiver_bound_instance_field",
                                            "owner_class": identity,
                                            "member": child.attr,
                                            "path": unit.path,
                                            "line": child.lineno,
                                            "producer": "exact owned constructor store",
                                        }
                        elif isinstance(method, ast.AnnAssign) and isinstance(method.target, ast.Name):
                            index[identity + "." + method.target.id] = {
                                "kind": "receiver_bound_dataclass_field",
                                "owner_class": identity,
                                "member": method.target.id,
                                "path": unit.path,
                                "line": method.lineno,
                                "producer": "owned generated frozen/slots dataclass constructor",
                            }
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        index[module + "." + target.id] = {"kind": "module_value_binding", "path": unit.path, "line": node.lineno}
    catalogue = contracts(h)
    runtime, annotations, imports = {}, {}, {}
    receiver_calls, captured_calls = [], []
    for unit in units:
        module = classifier["module_name"](unit.path)
        for qualified in catalogue.get(unit.path, {}):
            fn = h["_find"](unit.tree, qualified)
            if fn is None:
                continue
            bindings = classifier["imports_for"](unit, fn)
            definition_bindings = classifier["imports_for"](unit, fn, include_local=False)
            for identity, statement in bindings.values():
                imports.setdefault(identity, []).append({"path": unit.path, "line": statement.lineno})
            local = {arg.arg for arg in (*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs)}
            local.update(node.id for node in ast.walk(fn) if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)))
            local.update(node.name for node in ast.walk(fn) if isinstance(node, ast.ExceptHandler) and node.name is not None)
            visitor = classifier["ExecutionNames"]()
            for node in fn.body:
                visitor.visit(node)
            for name in visitor.names - local:
                identity = bindings[name][0] if name in bindings else "builtins." + name if name in vars(builtins) else module + "." + name
                runtime.setdefault(identity, []).append({"path": unit.path, "symbol": qualified, "line": fn.lineno, "scope": "body"})
            nonexecuting = [node.annotation for node in ast.walk(fn) if isinstance(node, ast.AnnAssign)]
            if h["_postponed_annotations"](unit):
                nonexecuting.extend(
                    arg.annotation for arg in (*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs) if arg.annotation is not None
                )
                nonexecuting.extend(
                    arg.annotation for arg in (fn.args.vararg, fn.args.kwarg) if arg is not None and arg.annotation is not None
                )
                if fn.returns is not None:
                    nonexecuting.append(fn.returns)
            # Definition expressions execute before body parameters or local
            # imports bind. Defaults/decorators execute in postponed mode too.
            definition_visitor = classifier["ExecutionNames"]()
            for expression in h["_definition_expressions"](unit, fn):
                definition_visitor.visit(expression)
            for name in definition_visitor.names:
                identity = (
                    definition_bindings[name][0]
                    if name in definition_bindings
                    else "builtins." + name
                    if name in vars(builtins)
                    else module + "." + name
                )
                runtime.setdefault(identity, []).append({"path": unit.path, "symbol": qualified, "line": fn.lineno, "scope": "definition"})
            for annotation in nonexecuting:
                for node in ast.walk(annotation):
                    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id not in local:
                        identity = (
                            definition_bindings[node.id][0]
                            if node.id in definition_bindings
                            else "builtins." + node.id
                            if node.id in vars(builtins)
                            else module + "." + node.id
                        )
                        annotations.setdefault(identity, []).append(
                            {"path": unit.path, "symbol": qualified, "line": fn.lineno, "scope": "nonexecuting annotation"}
                        )
            for node in ast.walk(fn):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    receiver_calls.append(
                        {
                            "path": unit.path,
                            "symbol": qualified,
                            "line": node.lineno,
                            "actual_receiver_ast": ast.dump(node.func.value),
                            "member": node.func.attr,
                            "scope": "runtime call",
                            "required_action": "resolve exact receiver origin and descriptor/callback effects in common graph; source shape is not physical completion",
                        }
                    )
                elif (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id in {"acquire", "release"}
                    and qualified == "ExecutionLeaseSQLSubmission.invoke"
                ):
                    captured_calls.append(
                        {
                            "path": unit.path,
                            "symbol": qualified,
                            "line": node.lineno,
                            "actual_callable": node.func.id,
                            "guarded_owner": "obligation.authority",
                            "guarded_function_origin": "elspeth.web.coordination.repository._SessionOperationAuthorityRepository."
                            + node.func.id,
                            "local_premise": "exact MethodType, __self__ and __func__ checks precede this actual call",
                            "required_action": "common graph must establish captured authority receiver and real supplier method effects; no SQL/physical completion claim",
                        }
                    )
    # All canonical method definitions run in the class suite, including
    # methods whose bodies are outside the selected operation catalogue.
    # Local grammar rejects class-local definition supplier shadowing; the
    # resolved module bindings still require the common origin/effect proof.
    for unit, qualified, fn in supplier_owned_method_definitions(units, h):
        module = classifier["module_name"](unit.path)
        definition_bindings = classifier["imports_for"](unit, fn, include_local=False)
        for identity, statement in definition_bindings.values():
            imports.setdefault(identity, []).append({"path": unit.path, "line": statement.lineno})
        visitor = classifier["ExecutionNames"]()
        for expression in h["_definition_expressions"](unit, fn):
            visitor.visit(expression)
        for name in visitor.names:
            identity = (
                definition_bindings[name][0]
                if name in definition_bindings
                else "builtins." + name
                if name in vars(builtins)
                else module + "." + name
            )
            runtime.setdefault(identity, []).append(
                {"path": unit.path, "symbol": qualified, "line": fn.lineno, "scope": "canonical class method definition"}
            )
    semantic = {
        "elspeth.contracts.session_operation." + name
        for name in ("SessionOperationContext", "SessionOperationFence", "SessionOperationKind")
    }
    semantic.update(
        "elspeth.web.execution_lease_cleanup." + name
        for name in (
            "ExecutionAcquisitionObligation",
            "ExecutionLeaseSQLSubmission",
            "ExecutionLeaseReleaseRegistry",
            "_ExecutionRegistration",
            "_ExecutionRetirement",
            "_ExecutionSQLArm",
            "_ISSUANCE_SEAL",
        )
    )
    semantic.update({"dataclasses.dataclass", "typing.final", "enum.StrEnum", "enum.Enum", "builtins.__build_class__", "builtins.object"})
    records = []
    for identity in sorted(roots):
        role = index.get(identity)
        if identity.startswith("elspeth.web.coordination.contracts.SessionOperation"):
            suffix = identity.rsplit(".", 1)[1]
            role = {
                "kind": "exact_local_reexport_binding",
                "canonical_target": "elspeth.contracts.session_operation." + suffix,
                "path": REEXPORT,
                "required_action": "common graph must prove stable canonical import and source origin",
            }
        if role is None:
            role = (
                {"kind": "immutable_builtin_referent_mutable_namespace_binding"}
                if identity.startswith("builtins.")
                else {"kind": "import_binding_target_requires_resolution", "import_sites": imports[identity]}
                if identity in imports
                else {"kind": "unresolved_source_identity"}
            )
        receiver = role["kind"].startswith("receiver_bound_")
        annotation_only = bool(annotations.get(identity)) and not runtime.get(identity) and not receiver and identity not in semantic
        records.append(
            {
                "identity": identity,
                **role,
                "executed_uses": runtime.get(identity, []),
                "nonexecuting_annotation_uses": annotations.get(identity, []),
                "semantic_producer_requirement": identity in semantic,
                "receiver_identity_required": receiver,
                "annotation_only": annotation_only,
                "origin_seed": not receiver and not annotation_only,
            }
        )
    return {
        "failures": failures,
        "records": records,
        "origin_seeds": [record["identity"] for record in records if record["origin_seed"]],
        "receiver_bound_members": [record for record in records if record["receiver_identity_required"]],
        "runtime_receiver_calls": receiver_calls,
        "captured_runtime_callable_transfers": captured_calls,
        "status": "PRIVATE_PARTIAL_NO_GO",
    }
