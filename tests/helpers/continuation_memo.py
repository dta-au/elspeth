"""Observe actual memo work without invoking producers or replacing aliases.

Profile call events measure attempted work, not return/outcome success. Native
tests own outcome claims. Budget assertions concern actual published DATA keys;
fallback attempts remain explicit and have no invented complete-input key.
Closed uncached validators are separately charged per native invocation and
stage. Scalar input characters are not detector traversal bytes.
"""

import builtins
import dis
import hashlib
import sys
from collections import Counter
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import _GeneratorContextManager, contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import datetime
from inspect import CO_ASYNC_GENERATOR, CO_COROUTINE, CO_GENERATOR, CO_VARARGS
from threading import Lock, get_ident
from types import BuiltinFunctionType, CellType, CodeType, FrameType, FunctionType, MethodType, TracebackType
from typing import Final, Literal
from weakref import ReferenceType, ref

import pytest
from sqlalchemy import event as sql_events
from sqlalchemy import util as sql_util
from sqlalchemy.dialects.postgresql.psycopg import PGCompiler_psycopg, PGDialect_psycopg, PGExecutionContext_psycopg
from sqlalchemy.dialects.sqlite.base import SQLiteCompiler, SQLiteExecutionContext
from sqlalchemy.dialects.sqlite.pysqlite import SQLiteDialect_pysqlite
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.engine.default import DefaultDialect, DefaultExecutionContext
from sqlalchemy.engine.events import ConnectionEvents
from sqlalchemy.sql import base as sql_base
from sqlalchemy.sql import crud as sql_crud
from sqlalchemy.sql import dml as sql_dml
from sqlalchemy.sql import functions as sql_functions
from sqlalchemy.sql import operators as sql_operators
from sqlalchemy.sql.compiler import SQLCompiler
from sqlalchemy.sql.dml import Insert, Update
from sqlalchemy.sql.elements import (
    BinaryExpression,
    BindParameter,
    BooleanClauseList,
    ClauseList,
    False_,
    Grouping,
    Null,
    True_,
    UnaryExpression,
    quoted_name,
)
from sqlalchemy.sql.schema import Column, Table
from sqlalchemy.sql.selectable import Select
from sqlalchemy.util import _collections as sql_collections
from sqlalchemy.util import immutabledict
from sqlalchemy.util import langhelpers as sql_langhelpers

from elspeth.contracts import bounded_serialization, credential_material, hashing, json_parser
from elspeth.plugins.infrastructure.base import BaseSink, BaseSource, BaseTransform
from elspeth.plugins.sources.llm.source import LLMSource
from elspeth.plugins.transforms.llm.transform import LLMTransform
from elspeth.web import async_workers, credential_guard
from elspeth.web import continuation_retention_memo as retention_memo
from elspeth.web.catalog import service as catalog_service
from elspeth.web.composer import (
    continuation_clock,
    continuation_codec,
    continuation_first_runtime,
    continuation_policy,
    diagnostic_evidence,
    provider_context,
)
from elspeth.web.composer import continuation_composition_initialization as composition_initialization
from elspeth.web.composer.continuation import PrivateChunkRef
from elspeth.web.continuation_retention_memo import (
    CredentialInputToken,
    CredentialPolicyKey,
    CredentialScanKey,
    PrivateChunkVerificationKey,
    RetentionStageMemo,
    SnapshotDecodeKey,
    StateCredentialStepKey,
    current_retention_stage_memo,
)
from elspeth.web.coordination import database_clock
from elspeth.web.required_executor import RequiredInvocationWitness
from elspeth.web.required_sql_outcomes import RequiredSQLRaised, RequiredSQLReturned
from elspeth.web.required_work import RequiredWorkTicket
from elspeth.web.sessions import continuation as stored_continuation
from elspeth.web.sessions import continuation_selection
from elspeth.web.sessions import engine as session_engine
from elspeth.web.sessions import locking as session_locking

_SNAPSHOT_REQUEST = continuation_codec.decode_continuation_snapshot.__code__
_CHUNK_REQUEST = continuation_codec.verify_private_chunk.__code__
_CREDENTIAL_REQUEST = credential_guard._retention_credential_finding.__code__
_STATE_REQUEST = credential_guard._retention_state_options_finding.__code__
_LOW_CODE_OBJECTS = (
    (continuation_codec._decode_continuation_snapshot_uncached.__code__, "snapshot_work"),
    (continuation_codec._verify_private_chunk_canonical.__code__, "chunk_work"),
    (credential_material.find_credential_material.__code__, "detector_work"),
)
_LOW_CODES = {id(code): label for code, label in _LOW_CODE_OBJECTS}
_PARSE = json_parser.parse_json_strict.__code__
_STRICT_PARSE = json_parser.parse_json_strict
_CHUNK_PRODUCER = continuation_codec._verify_private_chunk_canonical
_CHUNK_PUBLIC = continuation_codec.verify_private_chunk
_CHUNK_QUERY = RetentionStageMemo.private_chunk
_CHUNK_VALUE = retention_memo._chunk_value
_CHUNK_REFERENCE = continuation_codec._private_chunk_memo_reference
_CHUNK_WORK_CODE = _CHUNK_PRODUCER.__code__
_CHUNK_QUERY_CODE = _CHUNK_QUERY.__code__
_CHUNK_VALUE_CODE = _CHUNK_VALUE.__code__
_CHUNK_REFERENCE_CODE = _CHUNK_REFERENCE.__code__
_SNAPSHOT_PRODUCER = continuation_codec._decode_continuation_snapshot_uncached
_SNAPSHOT_QUERY = RetentionStageMemo.snapshot
_SNAPSHOT_VALUE = retention_memo._snapshot_value
_ORIGINAL_CANONICAL = hashing.canonical_json
_SNAPSHOT_POLICY = continuation_policy.decode_continuation_policy
_SNAPSHOT_WORK_CODE = _SNAPSHOT_PRODUCER.__code__
_SNAPSHOT_QUERY_CODE = _SNAPSHOT_QUERY.__code__
_SNAPSHOT_VALUE_CODE = _SNAPSHOT_VALUE.__code__
_SNAPSHOT_POLICY_CODE = _SNAPSHOT_POLICY.__code__
_SNAPSHOT_ARTIFACT_BASIS: Final = "post_observation_source_utf8_extent"
_CHUNK_ARTIFACT_BASIS: Final = "post_observation_source_utf8_extent"
ArtifactFamily = Literal["snapshot", "chunk"]
_CANONICAL = hashing.canonical_json.__code__
_QUERY_CODES = (
    RetentionStageMemo.snapshot.__code__,
    RetentionStageMemo.private_chunk.__code__,
    RetentionStageMemo.credential.__code__,
    RetentionStageMemo.state_credentials.__code__,
)
_SHA256 = hashlib.sha256
_METADATA_VALIDATOR = diagnostic_evidence.ComposerDiagnosticEvidence.__post_init__.__code__
_METADATA_POLICY = diagnostic_evidence._POLICY
_METADATA_VALIDATOR_NAME: Final = "diagnostic_correlation_metadata"
_CONTEXT_VALIDATOR = provider_context.capture_provider_context.__code__
_CAPTURE_CONTEXT = provider_context.capture_provider_context
_CONTEXT_CANONICAL_NAME: Final = "provider_context_capture"
_CONTEXT_CHECK_MESSAGES = provider_context._check_messages
_CONTEXT_CHECK_TOOLS = provider_context._check_tools
_CONTEXT_COPY_JSON = provider_context._copy_json
_CONTEXT_CANONICAL = provider_context.canonical_json
_CONTEXT_TEXT = provider_context._text.__code__
_CONTEXT_CONTENT = provider_context._message_content.__code__
_CONTEXT_MESSAGES = provider_context._check_messages.__code__
_CONTEXT_POLICY = provider_context._POLICY
_CONTEXT_VALIDATOR_NAME: Final = "provider_context_message_text"
_UNCACHED_VALIDATORS: Final = (_METADATA_VALIDATOR_NAME, _CONTEXT_VALIDATOR_NAME)
UncachedValidator = Literal["diagnostic_correlation_metadata", "provider_context_message_text"]
_BYTE_CHANNELS: Final = ("strict_parse", "canonical", "chunk_length", "chunk_digest", "scanner_size", "scanner_extent", "key_preparation")
_FIND_FUNCTION = credential_material._find
_REFERENCE_FUNCTION = credential_material._allowed_reference_finding
_FIND = _FIND_FUNCTION.__code__
_REFERENCE = _REFERENCE_FUNCTION.__code__
_UTF8_SIZE = bounded_serialization.utf8_size
_UTF8_CODE = _UTF8_SIZE.__code__
_UTF8_CHUNK_CHARACTERS = bounded_serialization._CHUNK_CHARACTERS
_REFERENCE_MAX_BYTES = credential_material._SECRET_REFERENCE_NAME_MAX_BYTES
_POLICY_NAMES = credential_guard._credential_policy_names.__code__
_TOKEN = next(
    code for code in credential_guard._credential_input_tokens.__code__.co_consts if type(code) is CodeType and code.co_name == "token"
)
_LOAD_SNAPSHOT = stored_continuation.load_snapshot.__code__
_LOAD_CHUNKS = continuation_selection.load_private_chunks.__code__
_LOAD_CHUNKS_FUNCTION = continuation_selection.load_private_chunks
_NESTED_CHUNKS_FUNCTION = continuation_selection.nested_private_chunks
_NESTED_CHUNKS = _NESTED_CHUNKS_FUNCTION.__code__
_PRIVATE_UTF8_LIMIT = continuation_selection.CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
_ENCODE_CHUNK = continuation_codec.encode_private_chunk.__code__
_RFC_DUMPS = hashing.rfc8785.dumps
_SNAPSHOT_RFC_CODE = _RFC_DUMPS.__code__ if type(_RFC_DUMPS) is FunctionType else None
_REGISTER_CALLBACK = sys.monitoring.register_callback
_CATALOG_CLASS = catalog_service.CatalogServiceImpl
_CATALOG_STAGE_CONTEXT = retention_memo._RETENTION_STAGE_MEMO
_CATALOG_MANAGER_CLASS = catalog_service.PluginManager
_CATALOG_FACTORY = _CATALOG_CLASS._catalog_schema
_CATALOG_FACTORY_CODE = _CATALOG_FACTORY.__code__
_CATALOG_READS = (
    catalog_service.CatalogServiceImpl.list_sources,
    catalog_service.CatalogServiceImpl.list_transforms,
    catalog_service.CatalogServiceImpl.list_sinks,
    catalog_service.CatalogServiceImpl.get_schema,
)
_CATALOG_READ_CODES = tuple(function.__code__ for function in _CATALOG_READS)
# Anchor independently of mutable per-Trace catalog/descriptor setup. Unknown
# overrides refuse qualification; original known bodies remain raw work even
# when current dispatch has been replaced before observation starts.
_CATALOG_SCHEMA_ORIGINALS = tuple(
    (owner, descriptor, descriptor.__func__, descriptor.__func__.__code__)
    for owner in (BaseSource, BaseTransform, BaseSink, LLMSource, LLMTransform)
    for descriptor in (owner.__dict__["get_config_schema"],)
)

# Independent original anchors: never learn an original from a mutable Trace
# setup or discover classes/registries while profiling application work.
_STRUCTURE_CLOCK = continuation_clock.ContinuationActionClock.run
_STRUCTURE_CLOCK_CLASS = continuation_clock.ContinuationActionClock
_STRUCTURE_ROOT = composition_initialization.initialize_continuation_composition
_STRUCTURE_HANDOFF_ROOT = continuation_first_runtime.restore_first_continuation_runtime
_STRUCTURE_WINDOW_FACTORY = session_locking.locked_session_transaction
_STRUCTURE_CONTEXT_FACTORY_CODE = _STRUCTURE_WINDOW_FACTORY.__code__
_STRUCTURE_CONTEXT_ENTER_CODE = _GeneratorContextManager.__enter__.__code__
_STRUCTURE_WINDOW = _STRUCTURE_WINDOW_FACTORY.__wrapped__
_STRUCTURE_DB_CLOCK = database_clock.database_now
_STRUCTURE_ADMISSION = stored_continuation.require_continuation_mutation
_STRUCTURE_SUBMIT = async_workers._submit_shared
_STRUCTURE_FINISH = async_workers.run_required_sql_finish_once
_STRUCTURE_EXECUTOR_SUBMIT = ThreadPoolExecutor.submit
_STRUCTURE_WITNESS = RequiredInvocationWitness.invoke
_STRUCTURE_PREPARE = retention_memo.prepare_retention_sql_callable
_STRUCTURE_SCOPE = retention_memo.retention_stage_memo_scope
_STRUCTURE_LOCK_BINDINGS = (
    ("process_session_lock", session_locking.process_session_lock),
    ("transaction_session_lock", session_locking.transaction_session_lock),
    ("sqlite_process_session_lock", session_locking.sqlite_process_session_lock),
    ("sqlite_transaction_session_lock", session_locking.sqlite_transaction_session_lock),
    ("acquire_session_advisory_xact_lock", session_locking.acquire_session_advisory_xact_lock),
)
_STRUCTURE_CONNECTION_EXECUTE = Connection.execute
_STRUCTURE_DRIVER_EXECUTE = Connection.exec_driver_sql
_STRUCTURE_ENGINE_FACTORY = session_engine.create_session_engine
_STRUCTURE_WRITE_OPTION = session_engine._SESSION_WRITE_INTENT_OPTION
_STRUCTURE_ENGINE_CONFIGURE = next(
    code for code in _STRUCTURE_ENGINE_FACTORY.__code__.co_consts if type(code) is CodeType and code.co_name == "_configure_sqlite_session"
)
_STRUCTURE_ENGINE_BEGIN = next(
    code for code in _STRUCTURE_ENGINE_FACTORY.__code__.co_consts if type(code) is CodeType and code.co_name == "_begin_session_write"
)
_STRUCTURE_ENGINE_TRANSACTION = next(
    code for code in _STRUCTURE_ENGINE_FACTORY.__code__.co_consts if type(code) is CodeType and code.co_name == "_begin_immediate"
)
_STRUCTURE_EXECUTION_INIT = DefaultExecutionContext._init_compiled.__func__
_STRUCTURE_STATEMENT_INIT = DefaultExecutionContext._init_statement.__func__
_STRUCTURE_SQLA_ENGINE_BEGIN = Engine.begin
_STRUCTURE_CONSTRUCT_PARAMS = SQLCompiler.construct_params
_STRUCTURE_SQL_DIGEST = hashlib.blake2b
_STRUCTURE_CURSOR_EXECUTE = DefaultDialect.do_execute
_STRUCTURE_CURSOR_NO_PARAMS = DefaultDialect.do_execute_no_params
_STRUCTURE_CURSOR_MANY = DefaultDialect.do_executemany
_STRUCTURE_SINGLE_CONTEXT = Connection._exec_single_context
_STRUCTURE_ROW_ADAPTER_CODE_REF = ref(
    next(
        code
        for code in ConnectionEvents.__dict__["_listen"].__func__.__code__.co_consts
        if type(code) is CodeType and code.co_name == "wrap_before_cursor_execute"
    )
)
_STRUCTURE_ADAPTER = next(code for code in _STRUCTURE_PREPARE.__code__.co_consts if type(code) is CodeType and code.co_name == "invoke")
_STRUCTURE_ADAPTER_RUN = next(code for code in _STRUCTURE_ADAPTER.co_consts if type(code) is CodeType and code.co_name == "run")
_STRUCTURE_ROLES = (
    (_STRUCTURE_CLOCK, "clock"),
    (_STRUCTURE_ROOT, "root"),
    (_STRUCTURE_HANDOFF_ROOT, "handoff_root"),
    (_STRUCTURE_WINDOW, "window"),
    (_STRUCTURE_DB_CLOCK, "db_clock"),
    (_STRUCTURE_ADMISSION, "admission"),
    (_STRUCTURE_SUBMIT, "submit"),
    (_STRUCTURE_FINISH, "finish"),
    (_STRUCTURE_EXECUTOR_SUBMIT, "executor"),
    (_STRUCTURE_CONNECTION_EXECUTE, "sql"),
    (_STRUCTURE_DRIVER_EXECUTE, "driver"),
    (_STRUCTURE_EXECUTION_INIT, "context_compiled"),
    (_STRUCTURE_STATEMENT_INIT, "context_statement"),
    (_STRUCTURE_CURSOR_EXECUTE, "cursor_execute"),
    (_STRUCTURE_CURSOR_NO_PARAMS, "cursor_no_params"),
    (_STRUCTURE_CURSOR_MANY, "cursor_many"),
)
_STRUCTURE_CODES = {id(function.__code__): (function.__code__, role) for function, role in _STRUCTURE_ROLES}
_STRUCTURE_LIFETIME_ROLES: Final = ("clock", "window", "root", "handoff_root", "submit", "finish")
_STRUCTURE_LIFETIME_CODES = tuple(function.__code__ for function, role in _STRUCTURE_ROLES if role in _STRUCTURE_LIFETIME_ROLES)
_LIFETIME_NAMESPACE = sys.monitoring
_LIFETIME_YIELD = sys.monitoring.events.PY_YIELD
_LIFETIME_RETURN = sys.monitoring.events.PY_RETURN
_LIFETIME_UNWIND = sys.monitoring.events.PY_UNWIND
_LIFETIME_FRAME = sys._getframe
_STRUCTURE_EXCEPTION_TRACEBACK = BaseException.__dict__["__traceback__"]
_STRUCTURE_EXCEPTION_TYPE = type
_STRUCTURE_EXCEPTION_TYPE_NAME = type.__dict__["__name__"]
_LIFETIME_GET_PROFILE = sys.getprofile
_LIFETIME_GET_TOOL = sys.monitoring.get_tool
_LIFETIME_USE_TOOL = sys.monitoring.use_tool_id
_LIFETIME_FREE_TOOL = sys.monitoring.free_tool_id
_LIFETIME_GET_EVENTS = sys.monitoring.get_events
_LIFETIME_SET_EVENTS = sys.monitoring.set_events
_LIFETIME_GET_LOCAL = sys.monitoring.get_local_events
_LIFETIME_SET_LOCAL = sys.monitoring.set_local_events
_LIFETIME_REGISTER = sys.monitoring.register_callback
_STRUCTURE_MONITOR_API = (
    ("get_tool", sys.monitoring.get_tool),
    ("use_tool_id", sys.monitoring.use_tool_id),
    ("free_tool_id", sys.monitoring.free_tool_id),
    ("get_events", sys.monitoring.get_events),
    ("set_events", sys.monitoring.set_events),
    ("get_local_events", sys.monitoring.get_local_events),
    ("set_local_events", sys.monitoring.set_local_events),
    ("register_callback", sys.monitoring.register_callback),
)
_STRUCTURE_CODES[id(_STRUCTURE_ENGINE_CONFIGURE)] = (_STRUCTURE_ENGINE_CONFIGURE, "connection_setup")
_STRUCTURE_SITES = {
    function.__code__: {instruction.offset: instruction.opname for instruction in dis.get_instructions(function.__code__)}
    for function, _role in _STRUCTURE_ROLES
}
_STRUCTURE_FUNCTION_CODES = tuple((function, function.__code__) for function, _role in _STRUCTURE_ROLES) + tuple(
    (function, function.__code__)
    for function in (
        _STRUCTURE_WITNESS,
        _STRUCTURE_PREPARE,
        _STRUCTURE_SCOPE,
        _STRUCTURE_WINDOW_FACTORY,
        _STRUCTURE_ENGINE_FACTORY,
        _STRUCTURE_EXECUTION_INIT,
        _STRUCTURE_STATEMENT_INIT,
        _STRUCTURE_SQLA_ENGINE_BEGIN,
        _STRUCTURE_CONSTRUCT_PARAMS,
        _STRUCTURE_SINGLE_CONTEXT,
        session_locking.process_session_lock,
        session_locking.transaction_session_lock,
        session_locking.sqlite_process_session_lock,
        session_locking.sqlite_transaction_session_lock,
        session_locking.acquire_session_advisory_xact_lock,
    )
)
_STRUCTURE_TABLES = (
    stored_continuation.sessions_table,
    stored_continuation.jobs,
    stored_continuation.attempts,
    stored_continuation.segments,
    stored_continuation.fences,
    stored_continuation.checkpoints,
    stored_continuation.chunks,
    stored_continuation.effects,
    stored_continuation.events,
    stored_continuation.ordinals,
    stored_continuation.states,
)
_STRUCTURE_COLUMNS = tuple(tuple(table.columns) for table in _STRUCTURE_TABLES)
# These are original owned schema facts, captured before mutable Trace setup.
# They borrow only installed metadata, never request parameters or SQL text.
_STRUCTURE_TABLE_FACTS = tuple((table, table.name, table.schema, table.named_with_column, table.fullname) for table in _STRUCTURE_TABLES)
_STRUCTURE_COLUMN_FACTS = tuple(
    (column, column.table, column.name, column.key, column.type, column.is_literal) for columns in _STRUCTURE_COLUMNS for column in columns
)
_STRUCTURE_IDENTIFIER_FACTS = tuple(
    (identifier, identifier.quote)
    for identifier in (
        *(table.name for table in _STRUCTURE_TABLES),
        *(table.schema for table in _STRUCTURE_TABLES),
        *(column.name for columns in _STRUCTURE_COLUMNS for column in columns),
        *(column.key for columns in _STRUCTURE_COLUMNS for column in columns),
    )
    if type(identifier) is quoted_name
)
_STRUCTURE_VISITOR_NAMES: Final = (
    "visit_column",
    "visit_select",
    "visit_binary",
    "visit_bindparam",
    "visit_expression_clauselist",
    "visit_grouping",
    "visit_unary",
    "visit_table",
    "visit_null",
    "visit_true",
    "visit_false",
)
_STRUCTURE_COMPILER_MROS = tuple((compiler, compiler.__mro__) for compiler in (SQLiteCompiler, PGCompiler_psycopg))
_STRUCTURE_COMPILER_OWNERS = tuple(dict.fromkeys(owner for _compiler, mro in _STRUCTURE_COMPILER_MROS for owner in mro))
_STRUCTURE_VISITOR_GUARDS = tuple(
    (owner, name, name in owner.__dict__, function, function.__code__ if type(function) is FunctionType else None)
    for owner in _STRUCTURE_COMPILER_OWNERS
    for name in _STRUCTURE_VISITOR_NAMES
    for function in (owner.__dict__.get(name),)
)
_STRUCTURE_OPERATORS = (
    sql_operators.eq,
    sql_operators.ne,
    sql_operators.gt,
    sql_operators.ge,
    sql_operators.lt,
    sql_operators.le,
    sql_operators.in_op,
    sql_operators.not_in_op,
    sql_operators.is_,
    sql_operators.is_not,
    sql_operators.and_,
    sql_operators.or_,
    sql_operators.asc_op,
    sql_operators.desc_op,
)
_STRUCTURE_DRIVER_LITERALS = tuple(
    (code, next(value for value in code.co_consts if type(value) is str and value == text), role)
    for code, text, role in (
        (_STRUCTURE_DB_CLOCK.__code__, "SELECT clock_timestamp()", "postgresql_clock"),
        (_STRUCTURE_DB_CLOCK.__code__, "SELECT CURRENT_TIMESTAMP", "sqlite_clock"),
        (_STRUCTURE_ENGINE_TRANSACTION, "BEGIN", "sqlite_begin"),
        (_STRUCTURE_ENGINE_TRANSACTION, "BEGIN IMMEDIATE", "sqlite_begin_immediate"),
        (
            session_locking.acquire_session_advisory_xact_lock.__code__,
            "SELECT pg_catalog.pg_advisory_xact_lock(%s, pg_catalog.hashtext(%s))",
            "advisory_lock",
        ),
    )
)


# These are original, finite publication sources, not a table/SQL registry.
_STRUCTURE_STORE_FUNCTIONS = tuple(
    (name, function, function.__code__, function.__globals__, count)
    for name, function, count in (
        ("put_chunk", stored_continuation.put_chunk, 2),
        ("append_complete_snapshot", stored_continuation.append_complete_snapshot, 3),
        ("load_snapshot", stored_continuation.load_snapshot, 3),
    )
)
_STRUCTURE_STORE_INSTRUCTIONS = tuple(
    tuple(dis.get_instructions(code, show_caches=True)) for _name, _function, code, _globals, _count in _STRUCTURE_STORE_FUNCTIONS
)
# Match the actual execute expression's code position, then that CALL's own
# inline-cache offsets. No neighbouring CALL, line heuristic or ancestor grant.
_STRUCTURE_STORE_CALLS = tuple(
    tuple(
        frozenset(
            item.offset
            for item in instructions
            if call.offset
            <= item.offset
            < next(
                (other.offset for other in instructions if other.offset > call.offset and other.opname != "CACHE"),
                len(code.co_code),
            )
        )
        for load in instructions
        if load.opname == "LOAD_ATTR" and load.argval == "execute"
        # A later result-method CALL can start at the same position as the
        # conn.execute(...) chain. Only the first matching CALL belongs to
        # this original execute attribute load; its caches end before that
        # result-method load/call. Missing correspondence fails source count.
        for call in (
            next(
                (
                    candidate
                    for candidate in instructions
                    if candidate.offset > load.offset
                    and candidate.opname in ("CALL", "CALL_KW")
                    and candidate.positions.lineno == load.positions.lineno
                    and candidate.positions.col_offset == load.positions.col_offset
                ),
                None,
            ),
        )
        if call is not None
    )
    for instructions, (_name, _function, code, _globals, _count) in zip(
        _STRUCTURE_STORE_INSTRUCTIONS, _STRUCTURE_STORE_FUNCTIONS, strict=True
    )
)
_STRUCTURE_STORE_ROLES = (
    (0, 1, Insert, stored_continuation.chunks, "chunk_insert"),
    (1, 0, Insert, stored_continuation.events, "event_insert"),
    (1, 1, Insert, stored_continuation.checkpoints, "checkpoint_insert"),
    (1, 2, Update, stored_continuation.jobs, "head_cas"),
    (2, 2, Select, None, "snapshot_sequence"),
)
_STRUCTURE_STORE_LAYOUTS = (
    (
        "chunk_insert",
        stored_continuation.chunks,
        ("session_id", "operation_id", "chunk_key", "payload", "sha256", "byte_length", "codec"),
        (),
    ),
    ("event_insert", stored_continuation.events, ("session_id", "operation_id", "sequence", "tag", "payload_chunk_key", "created_at"), ()),
    (
        "checkpoint_insert",
        stored_continuation.checkpoints,
        ("session_id", "operation_id", "checkpoint_id", "kind", "cursor", "snapshot_chunk_key", "frame_stack_version", "created_at"),
        (),
    ),
    (
        "head_cas",
        stored_continuation.jobs,
        ("checkpoint_id", "journal_cursor", "status_revision", "updated_at"),
        ("session_id", "operation_id", "lifecycle_version", "checkpoint_id", "journal_cursor"),
    ),
    ("snapshot_sequence", stored_continuation.events, (), ("session_id", "operation_id")),
)
_STRUCTURE_STORE_TABLE_FACTS = tuple(
    (table, table.columns, table.implicit_returning)
    for table in (stored_continuation.chunks, stored_continuation.events, stored_continuation.checkpoints, stored_continuation.jobs)
)
_STRUCTURE_STORE_COLUMN_FACTS = tuple(
    (
        column,
        column.default,
        column.onupdate,
        column.server_default,
        column.server_onupdate,
        column.primary_key,
        column.nullable,
        column.autoincrement,
    )
    for table, _columns, _returning in _STRUCTURE_STORE_TABLE_FACTS
    for column in table.columns
)
_STRUCTURE_STORE_CONSTRUCTORS = tuple(
    (name, function, function.__code__, class_name, constructor)
    for name, function, class_name, constructor in (
        ("insert", stored_continuation.insert, "Insert", Insert),
        ("update", stored_continuation.update, "Update", Update),
        ("select", stored_continuation.select, "Select", Select),
    )
)
_STRUCTURE_STORE_FUNC = stored_continuation.func
_STRUCTURE_STORE_FUNCTION_REGISTRY = sql_functions._registry
_STRUCTURE_STORE_DEFAULT_FUNCTIONS = sql_functions._registry.get("_default")
_STRUCTURE_STORE_COMPILE_PLUGINS = sql_base.CompileState.plugins
_STRUCTURE_STORE_CRUD_FUNCTIONS = tuple(
    (name, sql_crud.__dict__[name], sql_crud.__dict__[name].__code__)
    for name in (
        "_get_crud_params",
        "_key_getters_for_crud_column",
        "_get_stmt_parameter_tuples_params",
        "_scan_cols",
        "_append_param_parameter",
        "_handle_values_anonymous_param",
        "_create_bind_param",
        "_append_param_update",
        "_get_returning_modifiers",
    )
)
_STRUCTURE_STORE_MODULE_CLASSES = tuple(
    (module, name, module.__dict__[name])
    for module, name in (
        (sql_functions, "coalesce"),
        (sql_functions, "max"),
        (sql_functions, "_FunctionGenerator"),
        (sql_dml, "Insert"),
        (sql_dml, "Update"),
        (sql_dml, "InsertDMLState"),
        (sql_dml, "UpdateDMLState"),
        (sql_base, "CompileState"),
    )
)
_STRUCTURE_STORE_CLASSES = (
    Insert,
    Update,
    sql_dml.InsertDMLState,
    sql_dml.UpdateDMLState,
    sql_base.CompileState,
    sql_functions.coalesce,
    sql_functions.max,
    sql_functions._FunctionGenerator,
    ClauseList,
    Grouping,
    BindParameter,
    BinaryExpression,
)
_STRUCTURE_STORE_MROS = tuple((cls, cls.__mro__) for cls in _STRUCTURE_STORE_CLASSES)
_STRUCTURE_STORE_OWNERS = tuple(dict.fromkeys(owner for _cls, mro in _STRUCTURE_STORE_MROS for owner in mro))
_STRUCTURE_STORE_STATEMENT_SLOTS = (
    "__init__",
    "__getattribute__",
    "__getattr__",
    "_compiler_dispatch",
    "_compile_state_factory",
    "_effective_plugin_target",
    "_prefixes",
    "_hints",
    "_returning",
    "_return_defaults",
    "_return_defaults_columns",
    "_supplemental_returning",
    "_sort_by_parameter_order",
    "_inline",
    "_post_values_clause",
    "_multi_values",
    "_ordered_values",
    "_select_names",
    "_independent_ctes",
    "_independent_ctes_opts",
    "_with_options",
    "_annotations",
    "_propagate_attrs",
    "_execution_options",
)
_STRUCTURE_STORE_CLASS_SLOTS = (
    (Insert, _STRUCTURE_STORE_STATEMENT_SLOTS),
    (Update, _STRUCTURE_STORE_STATEMENT_SLOTS),
    (sql_dml.InsertDMLState, ("__init__", "create_for_statement", "_process_values", "_get_crud_kv_pairs")),
    (sql_dml.UpdateDMLState, ("__init__", "create_for_statement", "_process_values", "_make_extra_froms", "_get_crud_kv_pairs")),
    (sql_base.CompileState, ("__init__", "create_for_statement", "get_plugin_class")),
    (
        sql_functions.coalesce,
        (
            "__init__",
            "__getattribute__",
            "__getattr__",
            "_compiler_dispatch",
            "clause_expr",
            "name",
            "packagenames",
            "_with_ordinality",
            "_has_args",
            "type",
        ),
    ),
    (
        sql_functions.max,
        (
            "__init__",
            "__getattribute__",
            "__getattr__",
            "_compiler_dispatch",
            "clause_expr",
            "name",
            "packagenames",
            "_with_ordinality",
            "_has_args",
            "type",
        ),
    ),
    (sql_functions._FunctionGenerator, ("__init__", "__getattribute__", "__getattr__", "__call__")),
    (ClauseList, ("__init__", "__getattribute__", "__getattr__", "_compiler_dispatch", "clauses", "operator", "group", "group_contents")),
    (Grouping, ("__init__", "__getattribute__", "__getattr__", "_compiler_dispatch", "element")),
    (
        BindParameter,
        (
            "__init__",
            "__getattribute__",
            "__getattr__",
            "_compiler_dispatch",
            "value",
            "expanding",
            "literal_execute",
            "callable",
            "isoutparam",
        ),
    ),
    (BinaryExpression, ("__init__", "__getattribute__", "__getattr__", "_compiler_dispatch", "left", "right", "operator", "modifiers")),
)
_STRUCTURE_STORE_SLOT_PAIRS = tuple(
    dict.fromkeys((owner, name) for cls, names in _STRUCTURE_STORE_CLASS_SLOTS for owner in cls.__mro__ for name in names)
)
_STRUCTURE_STORE_SLOT_GUARDS = tuple(
    (owner, name, name in owner.__dict__, descriptor, function, function.__code__ if type(function) is FunctionType else None)
    for owner, name in _STRUCTURE_STORE_SLOT_PAIRS
    for descriptor in (owner.__dict__.get(name),)
    for function in (
        descriptor.__func__
        if type(descriptor) in (classmethod, staticmethod)
        else descriptor.fget
        if type(descriptor) is property
        else descriptor,
    )
)
_STRUCTURE_STORE_VISITOR_NAMES = (
    "visit_insert",
    "visit_update",
    "visit_function",
    "function_argspec",
    "visit_clauselist",
    "visit_coalesce_func",
    "visit_max_func",
)
_STRUCTURE_STORE_VISITOR_GUARDS = tuple(
    (owner, name, name in owner.__dict__, function, function.__code__ if type(function) is FunctionType else None)
    for owner in _STRUCTURE_COMPILER_OWNERS
    for name in _STRUCTURE_STORE_VISITOR_NAMES
    for function in (owner.__dict__.get(name),)
)
_STRUCTURE_STORE_ADD = sql_operators.add
_STRUCTURE_STORE_COMMA = sql_operators.comma_op

# Original descriptor state and finite defining-slot chains, held before any
# observation. This supports an EMPTY CURRENT cache, never its history/origin.
_STRUCTURE_CACHE_OWNER = sql_base.DialectKWArgs
_STRUCTURE_CACHE_DESCRIPTOR = _STRUCTURE_CACHE_OWNER.__dict__["dialect_options"]
_STRUCTURE_CACHE_DESCRIPTOR_CLASS = type(_STRUCTURE_CACHE_DESCRIPTOR)
_STRUCTURE_CACHE_DESCRIPTOR_FACTS = tuple(_STRUCTURE_CACHE_DESCRIPTOR.__dict__.items())
_STRUCTURE_CACHE_GETTER = _STRUCTURE_CACHE_DESCRIPTOR.fget
_STRUCTURE_CACHE_GETTER_CODE = _STRUCTURE_CACHE_GETTER.__code__
_STRUCTURE_CACHE_CREATOR_DESCRIPTOR = _STRUCTURE_CACHE_OWNER.__dict__["_kw_reg_for_dialect_cls"]
_STRUCTURE_CACHE_CREATOR = _STRUCTURE_CACHE_CREATOR_DESCRIPTOR.__func__
_STRUCTURE_CACHE_CLASS = sql_collections.PopulateDict
_STRUCTURE_CACHE_GENERIC_DESCRIPTOR = sql_langhelpers.generic_fn_descriptor
_STRUCTURE_CACHE_MROS = tuple(
    (cls, cls.__mro__)
    for cls in (Insert, Update, Select, _STRUCTURE_CACHE_CLASS, _STRUCTURE_CACHE_DESCRIPTOR_CLASS, _STRUCTURE_CACHE_GENERIC_DESCRIPTOR)
)
_STRUCTURE_CACHE_SLOT_PAIRS = tuple(
    dict.fromkeys(
        (owner, name)
        for cls, names in (
            (Insert, ("dialect_options", "_kw_reg_for_dialect_cls", "__dict__")),
            (Update, ("dialect_options", "_kw_reg_for_dialect_cls", "__dict__")),
            (Select, ("dialect_options", "_kw_reg_for_dialect_cls", "__dict__", "__getattribute__", "__getattr__")),
            (
                _STRUCTURE_CACHE_CLASS,
                (
                    "__init__",
                    "__missing__",
                    "__getattribute__",
                    "__getattr__",
                    "__setattr__",
                    "__delattr__",
                    "__dict__",
                    "__len__",
                    "__bool__",
                    "__iter__",
                    "__getitem__",
                    "__setitem__",
                ),
            ),
            (
                _STRUCTURE_CACHE_DESCRIPTOR_CLASS,
                ("__init__", "__get__", "__getattribute__", "__getattr__", "__setattr__", "__delattr__", "__dict__"),
            ),
        )
        for owner in cls.__mro__
        for name in names
    )
)
_STRUCTURE_CACHE_SLOT_GUARDS = tuple(
    (owner, name, name in owner.__dict__, descriptor, function, function.__code__ if type(function) is FunctionType else None)
    for owner, name in _STRUCTURE_CACHE_SLOT_PAIRS
    for descriptor in (owner.__dict__.get(name),)
    for function in (descriptor.__func__ if type(descriptor) in (classmethod, staticmethod) else descriptor,)
)
# Code presence records the immutable kind of these original held objects,
# never the validity of their current code or defining slot. Keep the original
# tuple/symbol identities so an exchanged fact cannot forge that kind marker.
_STRUCTURE_GUARD_KIND_ORIGINS = (
    type,
    FunctionType,
    _STRUCTURE_STORE_SLOT_GUARDS,
    _STRUCTURE_STORE_VISITOR_GUARDS,
    _STRUCTURE_VISITOR_GUARDS,
    _STRUCTURE_CACHE_SLOT_GUARDS,
)
# Independent original tuple binding, not validity. Its immutable expected kind
# operands may be borrowed; the actual symbols and tuple binding stay live.
_STRUCTURE_STORE_SLOT_KIND_ORIGINS = _STRUCTURE_GUARD_KIND_ORIGINS


@dataclass(frozen=True, slots=True)
class StructureSqlShape:
    kind: str
    tree: tuple
    bind_arities: tuple[int, ...]
    executemany: bool


@dataclass(frozen=True, slots=True)
class StructureEvent:
    ordinal: int
    kind: str
    phase: str
    clock: int | None
    stage: int | None
    expected_stage: int | None
    submission: int | None
    thread: int
    engine: int | None
    window: int | None
    connection: int | None
    source: str
    outcome: str | None = None
    result_kind: str | None = None
    sql: StructureSqlShape | None = None


@dataclass(frozen=True, slots=True)
class StructureLifetime:
    kind: str
    owner: int
    suspensions: int
    resumptions: int
    terminal: str


@dataclass(frozen=True, slots=True)
class StructureEdge:
    before: int
    after: int
    kind: str


@dataclass(frozen=True, slots=True)
class StructureSnapshot:
    version: int
    entrypoint: str
    required: bool
    closed: bool
    complete: bool
    observation_verified: bool
    errors: tuple[str, ...]
    events: tuple[StructureEvent, ...]
    edges: tuple[StructureEdge, ...]
    lifetimes: tuple[StructureLifetime, ...]


@dataclass(slots=True)
class _StructureInvocation:
    role: str
    event: int
    index: int
    phase: str
    clock: int | None
    stage: int | None
    memo: int | None
    parent: int | None
    engine: int | None = None
    connection: int | None = None
    statement: int | None = None
    driver_role: str | None = None
    suspended: bool = False
    suspensions: int = 0
    resumptions: int = 0
    listener_seen: bool = False
    listener_returned: bool = False
    submission: int | None = None
    execution_context: int | None = None
    sql_signature: tuple[int, bytes] | None = None
    driver_attempts: int = 0
    driver_outcome: str | None = None
    driver_source: str | None = None
    request: int | None = None
    pending_shape: StructureSqlShape | None = None
    pending_generation: int | None = None
    delivery_failed: bool = False
    store_role: str | None = None
    store_source_shape: tuple | None = None
    store_thread: int | None = None
    store_submission: int | None = None
    row_origin: "_StructureRowOrigin | None" = None
    row_dispatch: int | None = None
    row_dispatch_closed: bool = False
    row_generation: int = 0
    row_callbacks_started: int = 0
    row_callbacks_closed: int = 0
    row_callback: "_StructureRowCallback | None" = None


@dataclass(frozen=True, slots=True)
class _StructureRowOrigin:
    context: int
    container: int
    row: int
    cursor: int
    constructor: int
    code: int
    globals: int


class _StructureRowCodeIdentity(ReferenceType[CodeType]):
    """Weak code identity: CodeType's structural equality is never admission."""

    __slots__ = ("identity",)

    def __new__(cls, code: CodeType, callback=None):
        anchor = super().__new__(cls, code, callback)
        anchor.identity = id(code)
        return anchor

    def __hash__(self) -> int:
        return self.identity

    def __eq__(self, other: object) -> bool:
        if self is other:
            return True
        if type(other) is not _StructureRowCodeIdentity:
            return False
        code = self()
        return code is not None and code is other()

    def __ne__(self, other: object) -> bool:
        return not self.__eq__(other)


@dataclass(frozen=True, slots=True)
class _StructureRowCodeFacts:
    anchor: _StructureRowCodeIdentity
    positional_names: tuple[str, ...]
    vararg_name: str | None
    unsupported: bool
    return_sites: frozenset[int]


@dataclass(frozen=True, slots=True)
class _StructureRowCallback:
    generation: int
    request: int
    event: int
    origin: _StructureRowOrigin
    dispatch: int
    call_offset: int
    frame: int
    function: int
    receiver: int | None
    code: int
    globals: int
    thread: int
    hook: int
    carrier: tuple[int | None, int | None, bool | None]
    submission: int | None
    expected: int | None
    witness: int | None
    return_sites: frozenset[int]
    returned: bool = False
    outcome: str | None = None


@dataclass(frozen=True, slots=True)
class _StructureNativeEntry:
    kind: str
    frame: int
    code: int
    globals: int
    offset: int
    invocation: int
    generation: int
    event: int
    thread: int
    callback: int


@dataclass(frozen=True, slots=True)
class _StructurePendingReturn:
    generation: int
    frame: int
    code: int
    globals: int
    invocation: int
    event: int
    role: str
    thread: int
    hook: int
    carrier: tuple[int | None, int | None, bool | None]
    submission: int | None
    expected: int | None
    witness: int | None


@dataclass(slots=True)
class _StructureThread:
    identity: int
    counter: int = 0
    error_count: int = 0
    errors: list[str] = field(default_factory=list)
    internal_errors: list[tuple[str, str, int]] = field(default_factory=list)
    events: list[StructureEvent] = field(default_factory=list)
    edges: list[StructureEdge] = field(default_factory=list)
    lifetimes: list[StructureLifetime] = field(default_factory=list)
    frames: dict[int, _StructureInvocation] = field(default_factory=dict)
    clocks: list[int] = field(default_factory=list)
    windows: list[int] = field(default_factory=list)
    submission: int | None = None
    worker_clock: int | None = None
    worker_phase: str = "outside_root"
    expected: int | None = None
    worker_enter: int | None = None
    worker_terminal: int | None = None
    witness: int | None = None
    last: dict[tuple[str, int], int] = field(default_factory=dict)
    return_generation: int = 0
    pending_returns: dict[int, _StructurePendingReturn] = field(default_factory=dict)
    native_entry: _StructureNativeEntry | None = None
    row_generation: int = 0
    row_callbacks: dict[int, _StructureRowCallback] = field(default_factory=dict)
    row_returns: dict[int, _StructureRowCallback] = field(default_factory=dict)


def _structure_refuse(trace, code: str) -> None:
    thread = trace._structure_threads.get(get_ident())
    if thread is None:
        # Unsupported physical owner: a monotonic bucket, never a native work
        # count. Supported buffers below each have exactly one physical writer.
        trace._structure_error_count = 1
        if not trace._structure_errors:
            trace._structure_errors.append(code)
        return
    thread.error_count += 1
    if len(thread.errors) < 8:
        thread.errors.append(code)


def _structure_internal_error(trace, error: Exception) -> None:
    """Borrow only this catch's live traceback entry; retain primitive facts."""
    try:
        thread = trace._structure_threads.get(get_ident())
        if thread is None or len(thread.internal_errors) >= 8:
            return
        error_name = _STRUCTURE_EXCEPTION_TYPE_NAME.__get__(_STRUCTURE_EXCEPTION_TYPE(error))
        if _STRUCTURE_EXCEPTION_TYPE(error_name) is not _STRUCTURE_EXCEPTION_TYPE(""):
            error_name = "unavailable"
            trace._structure_observer_incomplete = True
        caller = _LIFETIME_FRAME(1)
        traceback = _STRUCTURE_EXCEPTION_TRACEBACK.__get__(error)
        if (
            _STRUCTURE_EXCEPTION_TYPE(traceback) is not TracebackType
            or traceback.tb_frame is not caller
            or caller.f_globals is not globals()
            or caller.f_code is not RetentionMemoTrace._structure_profile.__code__
        ):
            thread.internal_errors.append((error_name, "unbound", 0))
        else:
            code_name, line = caller.f_code.co_name, traceback.tb_lineno
            if _STRUCTURE_EXCEPTION_TYPE(code_name) is not _STRUCTURE_EXCEPTION_TYPE("") or _STRUCTURE_EXCEPTION_TYPE(
                line
            ) is not _STRUCTURE_EXCEPTION_TYPE(0):
                trace._structure_observer_incomplete = True
                thread.internal_errors.append((error_name, "unbound", 0))
            else:
                thread.internal_errors.append((error_name, code_name, line))
    except BaseException:
        # Diagnostic acquisition cannot replace an original application result
        # or a native ordinary/process-control primary. The original refusal is
        # already recorded, and unavailable provenance never qualifies.
        trace._structure_observer_incomplete = True


def _structure_number(thread: _StructureThread) -> int:
    # Injective pairing has no allocation/work ceiling and needs no shared
    # counter or reentrant observer lock. Each buffer has one physical writer.
    thread.counter += 1
    local = thread.counter
    total = thread.identity + local
    return total * (total + 1) // 2 + local


def _structure_parameter_arities(parameters, executemany: bool) -> tuple[int, ...]:
    if type(executemany) is not bool:
        raise ValueError("unknown parameter mode")
    if executemany:
        if type(parameters) not in (list, tuple) or any(type(row) not in (list, tuple, dict) for row in parameters):
            raise ValueError("unknown parameter rows")
        return tuple(len(row) for row in parameters)
    if type(parameters) not in (list, tuple, dict):
        raise ValueError("unknown parameter row")
    return (len(parameters),)


def _structure_sql_sources_current() -> bool:
    # Identity guards never invoke a mutable identifier's equality/protocol.
    if not _structure_store_cache_sources_current():
        return False
    for table, name, schema, named, fullname in _STRUCTURE_TABLE_FACTS:
        if (
            type(table) is not Table
            or table.name is not name
            or table.schema is not schema
            or table.named_with_column is not named
            or table.fullname is not fullname
        ):
            return False
    for column, table, name, key, datatype, literal in _STRUCTURE_COLUMN_FACTS:
        if (
            type(column) is not Column
            or column.table is not table
            or column.name is not name
            or column.key is not key
            or column.type is not datatype
            or column.is_literal is not literal
        ):
            return False
    for identifier, quote in _STRUCTURE_IDENTIFIER_FACTS:
        if identifier.quote is not quote:
            return False
    for compiler, mro in _STRUCTURE_COMPILER_MROS:
        if compiler.__mro__ is not mro:
            return False
    for sites, (_name, _function, _code, _globals, count) in zip(_STRUCTURE_STORE_CALLS, _STRUCTURE_STORE_FUNCTIONS, strict=True):
        if len(sites) != count:
            return False
    for name, function, code, namespace, _count in _STRUCTURE_STORE_FUNCTIONS:
        if stored_continuation.__dict__.get(name) is not function or function.__code__ is not code or function.__globals__ is not namespace:
            return False
    for name, function, code, class_name, constructor in _STRUCTURE_STORE_CONSTRUCTORS:
        if (
            stored_continuation.__dict__.get(name) is not function
            or function.__code__ is not code
            or function.__globals__.get(class_name) is not constructor
        ):
            return False
    for module, name, expected in _STRUCTURE_STORE_MODULE_CLASSES:
        if module.__dict__.get(name) is not expected:
            return False
    if (
        sql_operators.add is not _STRUCTURE_STORE_ADD
        or sql_operators.comma_op is not _STRUCTURE_STORE_COMMA
        or stored_continuation.func is not _STRUCTURE_STORE_FUNC
        or type(_STRUCTURE_STORE_FUNC.__dict__.get("opts")) is not dict
        or _STRUCTURE_STORE_FUNC.__dict__["opts"]
        or type(_STRUCTURE_STORE_FUNC.__dict__.get("_FunctionGenerator__names")) is not list
        or _STRUCTURE_STORE_FUNC.__dict__["_FunctionGenerator__names"]
        or sql_functions._registry is not _STRUCTURE_STORE_FUNCTION_REGISTRY
        or _STRUCTURE_STORE_FUNCTION_REGISTRY.get("_default") is not _STRUCTURE_STORE_DEFAULT_FUNCTIONS
        or type(_STRUCTURE_STORE_DEFAULT_FUNCTIONS) is not dict
        or _STRUCTURE_STORE_DEFAULT_FUNCTIONS.get("coalesce") is not sql_functions.coalesce
        or _STRUCTURE_STORE_DEFAULT_FUNCTIONS.get("max") is not sql_functions.max
        or sql_base.CompileState.plugins is not _STRUCTURE_STORE_COMPILE_PLUGINS
        or _STRUCTURE_STORE_COMPILE_PLUGINS.get(("default", "insert")) is not sql_dml.InsertDMLState
        or _STRUCTURE_STORE_COMPILE_PLUGINS.get(("default", "update")) is not sql_dml.UpdateDMLState
    ):
        return False
    for name, function, code in _STRUCTURE_STORE_CRUD_FUNCTIONS:
        if sql_crud.__dict__.get(name) is not function or type(function) is not FunctionType or function.__code__ is not code:
            return False
    for cls, mro in _STRUCTURE_STORE_MROS:
        if cls.__mro__ is not mro:
            return False
    for table, columns, returning in _STRUCTURE_STORE_TABLE_FACTS:
        if table.columns is not columns or table.implicit_returning is not returning:
            return False
    for column, default, onupdate, server_default, server_onupdate, primary, nullable, autoincrement in _STRUCTURE_STORE_COLUMN_FACTS:
        if (
            column.default is not default
            or column.onupdate is not onupdate
            or column.server_default is not server_default
            or column.server_onupdate is not server_onupdate
            or column.primary_key is not primary
            or column.nullable is not nullable
            or column.autoincrement is not autoincrement
        ):
            return False
    guards = _STRUCTURE_STORE_SLOT_GUARDS
    if guards is not _STRUCTURE_GUARD_KIND_ORIGINS[2]:
        return False
    origins = _STRUCTURE_STORE_SLOT_KIND_ORIGINS
    if _STRUCTURE_GUARD_KIND_ORIGINS is not origins:
        return False
    original_type, original_function_type = origins[0], origins[1]
    for owner, name, present, descriptor, function, code in guards:
        if (
            (name in owner.__dict__) is not present
            or owner.__dict__.get(name) is not descriptor
            or (
                type is not original_type
                or FunctionType is not original_function_type
                or _STRUCTURE_GUARD_KIND_ORIGINS is not origins
                or (code is not None and function.__code__ is not code)
            )
        ):
            return False
    guards = _STRUCTURE_STORE_VISITOR_GUARDS
    if guards is not _STRUCTURE_GUARD_KIND_ORIGINS[3]:
        return False
    for owner, name, present, function, code in guards:
        if (
            (name in owner.__dict__) is not present
            or owner.__dict__.get(name) is not function
            or (
                present
                and (
                    type is not _STRUCTURE_GUARD_KIND_ORIGINS[0]
                    or FunctionType is not _STRUCTURE_GUARD_KIND_ORIGINS[1]
                    or code is None
                    or function.__code__ is not code
                )
            )
        ):
            return False
    guards = _STRUCTURE_VISITOR_GUARDS
    if guards is not _STRUCTURE_GUARD_KIND_ORIGINS[4]:
        return False
    for owner, name, present, function, code in guards:
        if not (
            (name in owner.__dict__) is present
            and owner.__dict__.get(name) is function
            and (
                not present
                or (
                    type is _STRUCTURE_GUARD_KIND_ORIGINS[0]
                    and FunctionType is _STRUCTURE_GUARD_KIND_ORIGINS[1]
                    and code is not None
                    and function.__code__ is code
                )
            )
        ):
            return False
    return True


def _structure_sql_signature(statement) -> tuple[int, bytes]:
    if type(statement) is not str:
        raise ValueError("unsupported statement representation")
    # Explicit observer provenance work, separate from every native byte/hash
    # channel. Only this fixed-size signature survives the callback.
    encoded = statement.encode("utf-8", "strict")
    return len(encoded), _STRUCTURE_SQL_DIGEST(encoded, digest_size=32).digest()


_STRUCTURE_SHAPE_GUARD_CODES: Final = frozenset(
    (
        "source_current",
        "source_role",
        "source_type",
        "source_dispatch",
        "source_compiler_dispatch",
        "source_compile_state_factory",
        "source_cache_creator_shadow",
        "source_cache_source",
        "source_cache_type",
        "source_cache_nonempty",
        "source_cache_state",
        "source_cache_creator",
        "source_clauses",
        "source_options",
        "source_columns",
        "source_bind",
        "source_constant",
        "source_predicates",
        "source_predicate_expression",
        "sequence_clauses",
        "sequence_mode",
        "sequence_select",
        "sequence_outer",
        "sequence_arguments",
        "sequence_outer_arity",
        "sequence_function",
        "sequence_inner_arguments",
        "sequence_column",
        "sequence_grouping",
        "source_dml_container",
        "source_dml_alternatives",
        "source_insert_mode",
        "source_assignments",
        "source_revision",
        "source_bind_coverage",
        "dml_origin",
        "dml_carrier",
        "dml_window",
        "dml_pre_post",
        "dml_compiler",
        "dml_compile_state",
        "dml_cache",
        "dml_bind",
        "dml_clone_map",
        "dml_clone_pair",
        "dml_rendered_pair",
        "dml_rendered_source",
        "dml_extra_rendered",
        "dml_bind_coverage",
        "dml_current_rows",
        "dml_current_constant",
        "dml_final_row",
        "dml_final_arity",
        "dml_final_names",
        "dml_final_correspondence",
        "dml_final_constant",
        "sequence_origin",
        "dml_bind_name",
    )
)


class _StructureShapeRefusal(Exception):
    """An owned guard code only; no native error or value payload."""

    __slots__ = ("guard_code",)

    def __init__(self, guard_code: str) -> None:
        if type(guard_code) is not str or guard_code not in _STRUCTURE_SHAPE_GUARD_CODES:
            raise ValueError("unknown owned structure guard")
        self.guard_code = guard_code
        super().__init__()


def _structure_store_cache_sources_current() -> bool:
    """Static held source facts only; never inspect a current SQL/cache object."""
    if (
        sql_base.DialectKWArgs is not _STRUCTURE_CACHE_OWNER
        or sql_util.PopulateDict is not _STRUCTURE_CACHE_CLASS
        or sql_collections.PopulateDict is not _STRUCTURE_CACHE_CLASS
        or sql_langhelpers.memoized_property is not _STRUCTURE_CACHE_DESCRIPTOR_CLASS
        or sql_langhelpers._memoized_property is not _STRUCTURE_CACHE_DESCRIPTOR_CLASS
        or sql_langhelpers.generic_fn_descriptor is not _STRUCTURE_CACHE_GENERIC_DESCRIPTOR
        or type(_STRUCTURE_CACHE_DESCRIPTOR) is not _STRUCTURE_CACHE_DESCRIPTOR_CLASS
        or type(_STRUCTURE_CACHE_GETTER) is not FunctionType
        or _STRUCTURE_CACHE_GETTER.__code__ is not _STRUCTURE_CACHE_GETTER_CODE
        or _STRUCTURE_CACHE_GETTER.__globals__ is not sql_base.__dict__
        or sql_base.__dict__.get("util") is not sql_util
    ):
        return False
    for cls, mro in _STRUCTURE_CACHE_MROS:
        if cls.__mro__ is not mro:
            return False
    guards = _STRUCTURE_CACHE_SLOT_GUARDS
    if guards is not _STRUCTURE_GUARD_KIND_ORIGINS[5]:
        return False
    for owner, name, present, descriptor, function, code in guards:
        if (
            (name in owner.__dict__) is not present
            or owner.__dict__.get(name) is not descriptor
            or (
                type is not _STRUCTURE_GUARD_KIND_ORIGINS[0]
                or FunctionType is not _STRUCTURE_GUARD_KIND_ORIGINS[1]
                or (code is not None and function.__code__ is not code)
            )
        ):
            return False
    descriptor_state = object.__getattribute__(_STRUCTURE_CACHE_DESCRIPTOR, "__dict__")
    if type(descriptor_state) is not dict or dict.__len__(descriptor_state) != len(_STRUCTURE_CACHE_DESCRIPTOR_FACTS):
        return False
    for key in dict.keys(descriptor_state):
        if type(key) is not str:
            return False
    for name, expected in _STRUCTURE_CACHE_DESCRIPTOR_FACTS:
        if name not in descriptor_state or descriptor_state[name] is not expected:
            return False
    return True


def _structure_store_empty_dialect_cache(statement: object) -> None:
    """Validate only the nominal empty current cache, without creating it."""
    if not _structure_store_cache_sources_current():
        raise _StructureShapeRefusal("source_cache_source")
    state = object.__getattribute__(statement, "__dict__")
    for name, code in (
        ("_compiler_dispatch", "source_compiler_dispatch"),
        ("_compile_state_factory", "source_compile_state_factory"),
        ("_kw_reg_for_dialect_cls", "source_cache_creator_shadow"),
    ):
        if name in state:
            raise _StructureShapeRefusal(code)
    if "dialect_options" not in state:
        return
    cache = state["dialect_options"]
    if type(statement) not in (Insert, Update) or type(cache) is not _STRUCTURE_CACHE_CLASS:
        raise _StructureShapeRefusal("source_cache_type")
    if dict.__len__(cache) != 0:
        raise _StructureShapeRefusal("source_cache_nonempty")
    cache_state = object.__getattribute__(cache, "__dict__")
    if (
        type(cache_state) is not dict
        or dict.__len__(cache_state) != 1
        or any(type(key) is not str for key in dict.keys(cache_state))
        or "creator" not in cache_state
    ):
        raise _StructureShapeRefusal("source_cache_state")
    creator = cache_state["creator"]
    if type(creator) is not MethodType or creator.__self__ is not type(statement) or creator.__func__ is not _STRUCTURE_CACHE_CREATOR:
        raise _StructureShapeRefusal("source_cache_creator")


def _structure_store_role(frame: FrameType, statement: object) -> str | None:
    caller = frame.f_back
    if frame.f_code is not _STRUCTURE_CONNECTION_EXECUTE.__code__ or caller is None:
        return None
    for source, site, statement_type, table, role in _STRUCTURE_STORE_ROLES:
        name, function, code, namespace, count = _STRUCTURE_STORE_FUNCTIONS[source]
        sites = _STRUCTURE_STORE_CALLS[source]
        if (
            len(sites) == count
            and caller.f_code is code
            and caller.f_globals is namespace
            and stored_continuation.__dict__.get(name) is function
            and function.__code__ is code
            and caller.f_lasti in sites[site]
            and type(statement) is statement_type
            and (table is None or statement.table is table)
        ):
            return role
    return None


def _structure_store_source_shape(statement: object, role: str) -> tuple:
    """Borrow the live statement; return only closed structure/numeric bind IDs.

    The IDs are consumed immediately while this exact statement is live; they
    are NEVER stored in the raw invocation or exported. Only the first tuple,
    with local bind ordinals and fixed source tags, survives observation.
    """
    if not _structure_sql_sources_current():
        raise _StructureShapeRefusal("source_cache_source" if not _structure_store_cache_sources_current() else "source_current")
    layout = next((item for item in _STRUCTURE_STORE_LAYOUTS if item[0] == role), None)
    if layout is None:
        raise _StructureShapeRefusal("source_role")
    _role, table, assignments, predicates = layout
    expected_type = Select if role == "snapshot_sequence" else Update if role == "head_cas" else Insert
    if type(statement) is not expected_type:
        raise _StructureShapeRefusal("source_type")
    _structure_store_empty_dialect_cache(statement)
    for collection in (statement._prefixes, statement._independent_ctes, statement._independent_ctes_opts, statement._with_options):
        if type(collection) is not tuple or collection:
            raise _StructureShapeRefusal("source_clauses")
    for collection in (statement._annotations, statement._propagate_attrs, statement._execution_options):
        if type(collection) not in (dict, immutabledict) or collection:
            raise _StructureShapeRefusal("source_options")
    table_index = next(index for index, known in enumerate(_STRUCTURE_TABLES) if table is known)
    if any(current is not expected for current, expected in zip(table.columns, _STRUCTURE_COLUMNS[table_index], strict=True)):
        raise _StructureShapeRefusal("source_columns")
    bind_ids = []
    fixed = []

    def bind(node, tag=None):
        if (
            type(node) is not BindParameter
            or node.expanding is not False
            or node.literal_execute is not False
            or node.callable is not None
            or node.isoutparam is not False
        ):
            raise _StructureShapeRefusal("source_bind")
        identity = id(node)
        if identity not in bind_ids:
            bind_ids.append(identity)
        ordinal = bind_ids.index(identity)
        if tag is not None:
            expected = 0 if tag == "zero" else 2 if tag == "lifecycle_two" else 1
            if type(node.value) is not int or node.value != expected:
                raise _StructureShapeRefusal("source_constant")
            fixed.append((ordinal, tag))
        return ("bind", ordinal, tag)

    def column(name):
        return next(known for known in _STRUCTURE_COLUMNS[table_index] if known.key == name)

    def where(clauses):
        if type(clauses) is not tuple or len(clauses) != len(predicates):
            raise _StructureShapeRefusal("source_predicates")
        result = []
        for clause, name in zip(clauses, predicates, strict=True):
            if (
                type(clause) is not BinaryExpression
                or clause.left is not column(name)
                or clause.operator is not _STRUCTURE_OPERATORS[0]
                or clause.modifiers
            ):
                raise _StructureShapeRefusal("source_predicate_expression")
            tag = "lifecycle_two" if role == "head_cas" and name == "lifecycle_version" else None
            result.append(
                (
                    "eq",
                    next(index for index, known in enumerate(_STRUCTURE_COLUMNS[table_index]) if known is clause.left),
                    bind(clause.right, tag),
                )
            )
        return tuple(result)

    if role == "snapshot_sequence":
        for collection in (
            statement._suffixes,
            statement._statement_hints,
            statement._distinct_on,
            statement._group_by_clauses,
            statement._having_criteria,
            statement._setup_joins,
            statement._correlate,
            statement._from_obj,
            statement._order_by_clauses,
        ):
            if type(collection) is not tuple or collection:
                raise _StructureShapeRefusal("sequence_clauses")
        if type(statement._hints) not in (dict, immutabledict) or statement._hints or statement._distinct is not False:
            raise _StructureShapeRefusal("sequence_mode")
        if (
            statement._suffixes
            or statement._hints
            or statement._statement_hints
            or statement._distinct
            or statement._distinct_on
            or statement._for_update_arg is not None
            or statement._group_by_clauses
            or statement._having_criteria
            or statement._setup_joins
            or statement._correlate
            or statement._correlate_except is not None
            or statement._fetch_clause is not None
            or statement._from_obj
            or statement._order_by_clauses
            or statement._limit_clause is not None
            or statement._offset_clause is not None
            or type(statement._raw_columns) not in (list, tuple)
            or len(statement._raw_columns) != 1
        ):
            raise _StructureShapeRefusal("sequence_select")
        outer = statement._raw_columns[0]
        if (
            type(outer) is not sql_functions.coalesce
            or type(outer.name) is not str
            or outer.name != "coalesce"
            or type(outer.packagenames) is not tuple
            or outer.packagenames
            or outer._with_ordinality is not False
        ):
            raise _StructureShapeRefusal("sequence_outer")
        if type(outer.clause_expr) is not Grouping or type(outer.clause_expr.element) is not ClauseList:
            raise _StructureShapeRefusal("sequence_arguments")
        args = outer.clause_expr.element
        if args.operator is not _STRUCTURE_STORE_COMMA or type(args.clauses) is not list or len(args.clauses) != 2:
            raise _StructureShapeRefusal("sequence_outer_arity")
        maximum, default = args.clauses
        if (
            type(maximum) is not sql_functions.max
            or type(maximum.name) is not str
            or maximum.name != "max"
            or type(maximum.packagenames) is not tuple
            or maximum.packagenames
            or maximum._with_ordinality is not False
        ):
            raise _StructureShapeRefusal("sequence_function")
        if type(maximum.clause_expr) is not Grouping or type(maximum.clause_expr.element) is not ClauseList:
            raise _StructureShapeRefusal("sequence_inner_arguments")
        inner = maximum.clause_expr.element
        if (
            inner.operator is not _STRUCTURE_STORE_COMMA
            or type(inner.clauses) is not list
            or len(inner.clauses) != 1
            or inner.clauses[0] is not stored_continuation.events.c.sequence
        ):
            raise _StructureShapeRefusal("sequence_column")
        if (
            args.group is not True
            or args.group_contents is not True
            or inner.group is not True
            or inner.group_contents is not True
            or outer.type is not column("sequence").type
            or maximum.type is not column("sequence").type
        ):
            raise _StructureShapeRefusal("sequence_grouping")
        function = ("coalesce_max_sequence", bind(default, "zero"))
        tree = (role, table_index, function, where(statement._where_criteria))
    else:
        if (
            type(statement._returning) is not tuple
            or type(statement._multi_values) is not tuple
            or type(statement._hints) not in (dict, immutabledict)
        ):
            raise _StructureShapeRefusal("source_dml_container")
        if (
            statement.table is not table
            or statement._returning
            or statement._return_defaults is not False
            or statement._return_defaults_columns is not None
            or statement._supplemental_returning is not None
            or statement._inline is not False
            or statement._post_values_clause is not None
            or statement._multi_values
            or statement._ordered_values is not None
            or statement._select_names is not None
            or statement._hints
        ):
            raise _StructureShapeRefusal("source_dml_alternatives")
        if type(statement) is Insert and (statement.select is not None or statement._sort_by_parameter_order is not False):
            raise _StructureShapeRefusal("source_insert_mode")
        values = statement._values
        if type(values) is not immutabledict or any(type(key) is not str for key in values) or tuple(values) != assignments:
            raise _StructureShapeRefusal("source_assignments")
        items = []
        for name in assignments:
            value = values[name]
            if role == "head_cas" and name == "status_revision":
                if (
                    type(value) is not BinaryExpression
                    or value.left is not column(name)
                    or value.operator is not _STRUCTURE_STORE_ADD
                    or value.modifiers
                ):
                    raise _StructureShapeRefusal("source_revision")
                expression = ("increment_revision", bind(value.right, "revision_one"))
            else:
                expression = bind(value, "frame_version_one" if role == "checkpoint_insert" and name == "frame_stack_version" else None)
            ordinal = next(index for index, known in enumerate(_STRUCTURE_COLUMNS[table_index]) if known is column(name))
            items.append((ordinal, expression))
        tree = (role, table_index, tuple(items), where(statement._where_criteria) if role == "head_cas" else ())
    expected_binds = len(assignments) + len(predicates) + int(role == "snapshot_sequence")
    if len(bind_ids) != expected_binds:
        raise _StructureShapeRefusal("source_bind_coverage")
    return tree, tuple(bind_ids), tuple(fixed)


def _structure_dml_shape(trace, owner: _StructureInvocation, context, *, final_parameters=...) -> StructureSqlShape | None:
    """Closed publication layouts, sharing only numeric binding with aggregate."""
    if owner.store_role is None or owner.store_source_shape is None or id(context.invoked_statement) != owner.statement:
        raise _StructureShapeRefusal("dml_origin")
    thread = trace._structure_threads.get(get_ident())
    carrier = trace._structure_carrier()
    expected_carrier = (owner.memo, owner.stage, False if owner.memo is not None else None)
    if thread is None or owner.store_thread != get_ident() or owner.store_submission != thread.submission or carrier != expected_carrier:
        raise _StructureShapeRefusal("dml_carrier")
    actual_clock = thread.frames[thread.clocks[-1]].event if thread.clocks else thread.worker_clock
    if actual_clock != owner.clock or not any(
        current.role == "window" and current.event == owner.parent and current.connection == owner.connection
        for current in thread.frames.values()
    ):
        raise _StructureShapeRefusal("dml_window")
    tree, bind_ids, fixed = _structure_store_source_shape(context.invoked_statement, owner.store_role)
    if tree != owner.store_source_shape or context.executemany is not False:
        raise _StructureShapeRefusal("dml_pre_post")
    compiled = context.compiled
    compiler = SQLiteCompiler if type(context) is SQLiteExecutionContext else PGCompiler_psycopg
    if (
        type(compiled) is not compiler
        or compiled.literal_execute_params
        or any(name in compiled.__dict__ for name in ("construct_params", *_STRUCTURE_VISITOR_NAMES, *_STRUCTURE_STORE_VISITOR_NAMES))
    ):
        raise _StructureShapeRefusal("dml_compiler")
    if owner.store_role != "snapshot_sequence":
        state_type = sql_dml.UpdateDMLState if owner.store_role == "head_cas" else sql_dml.InsertDMLState
        if type(compiled.compile_state) is not state_type or type(compiled.dml_compile_state) is not state_type:
            raise _StructureShapeRefusal("dml_compile_state")
    extracted = context.extracted_parameters
    if extracted is None:
        pairs = tuple((node, node) for node in compiled.bind_names)
    else:
        if compiled.cache_key is None or len(extracted) != len(compiled.cache_key.bindparams):
            raise _StructureShapeRefusal("dml_cache")
        pairs = tuple(zip(extracted, compiled.cache_key.bindparams, strict=True))
    if any(
        type(node) is not BindParameter
        or type(cached) is not BindParameter
        or node.expanding
        or cached.expanding
        or node.literal_execute
        or cached.literal_execute
        for node, cached in pairs
    ):
        raise _StructureShapeRefusal("dml_bind")
    # Original CRUD may clone a NULL-typed source bind to the owned column
    # type (the genuine malformed-codec driver control exercises this). Reuse
    # the SAME compilation's original cache-key clone correspondence, never a
    # new compile, guessed parameter or cached compiled.params value.
    matches = compiled._cache_key_bind_match
    names = {}
    rendered_ids = set()
    for node, cached in pairs:
        aliases = ()
        if matches is not None:
            if type(matches) is not tuple or len(matches) != 2 or type(matches[0]) is not dict:
                raise _StructureShapeRefusal("dml_clone_map")
            aliases = next((items for original, items in matches[0].items() if original is cached), ())
            if type(aliases) is not list:
                raise _StructureShapeRefusal("dml_clone_pair")
        candidates = tuple(
            (rendered, name)
            for rendered, name in compiled.bind_names.items()
            if rendered is cached or any(rendered is alias for alias in aliases)
        )
        if len(candidates) != 1:
            raise _StructureShapeRefusal("dml_rendered_pair")
        rendered, name = candidates[0]
        if type(rendered) is not BindParameter or rendered.expanding or rendered.literal_execute or rendered.callable is not None:
            raise _StructureShapeRefusal("dml_rendered_source")
        names[id(node)] = name
        rendered_ids.add(id(rendered))
    if rendered_ids != {id(node) for node in compiled.bind_names}:
        raise _StructureShapeRefusal("dml_extra_rendered")
    if set(names) != set(bind_ids):
        raise _StructureShapeRefusal("dml_bind_coverage")
    # Only direct assignment binds obtain the held key of their SPECIFIC
    # original column. Predicates, revision expressions and aggregate defaults
    # retain generated builtin-str names; no all-owned-key or equality grant.
    assignment_keys = {}
    if owner.store_role != "snapshot_sequence":
        for column_ordinal, expression in tree[2]:
            if expression[0] == "bind":
                column = _STRUCTURE_COLUMNS[tree[1]][column_ordinal]
                held_key = next(key for known, _table, _name, key, _datatype, _literal in _STRUCTURE_COLUMN_FACTS if known is column)
                assignment_keys[bind_ids[expression[1]]] = held_key
    if any(
        type(name) is not str and not (type(name) is quoted_name and name is assignment_keys.get(identity))
        for identity, name in names.items()
    ):
        _structure_refuse(trace, "publication_bind_name_provenance")
        raise _StructureShapeRefusal("dml_bind_name")
    rows = context.compiled_parameters
    if type(rows) is not list or len(rows) != 1 or type(rows[0]) is not dict or set(rows[0]) != set(names.values()):
        raise _StructureShapeRefusal("dml_current_rows")
    for ordinal, tag in fixed:
        value = rows[0][names[bind_ids[ordinal]]]
        expected = 0 if tag == "zero" else 2 if tag == "lifecycle_two" else 1
        if type(value) is not int or value != expected:
            raise _StructureShapeRefusal("dml_current_constant")
    if final_parameters is not ...:
        # The original context owns the processed driver row. A retval row
        # replacement, even an equal-looking copy, has no original identity
        # proof in this closed family. Inspect no opaque parameter values.
        processed = context.parameters
        if type(processed) not in (tuple, list) or len(processed) != 1 or final_parameters is not processed[0]:
            raise _StructureShapeRefusal("dml_final_row")
        if _structure_parameter_arities(final_parameters, False) != (len(bind_ids),):
            raise _StructureShapeRefusal("dml_final_arity")
        positions = compiled.positiontup
        for ordinal, tag in fixed:
            name = names[bind_ids[ordinal]]
            if type(final_parameters) is dict:
                if set(final_parameters) != set(names.values()):
                    raise _StructureShapeRefusal("dml_final_names")
                value = final_parameters[name]
            elif (
                type(final_parameters) in (tuple, list)
                and type(positions) is list
                and len(positions) == len(bind_ids)
                and set(positions) == set(names.values())
            ):
                value = final_parameters[positions.index(name)]
            else:
                raise _StructureShapeRefusal("dml_final_correspondence")
            expected = 0 if tag == "zero" else 2 if tag == "lifecycle_two" else 1
            if type(value) is not int or value != expected:
                raise _StructureShapeRefusal("dml_final_constant")
    return StructureSqlShape(
        "select" if owner.store_role == "snapshot_sequence" else "update" if owner.store_role == "head_cas" else "insert", tree, (), False
    )


def _structure_snapshot_sequence_shape(trace, owner: _StructureInvocation, context, node) -> StructureSqlShape | None:
    if owner.store_role != "snapshot_sequence" or node is not context.invoked_statement or type(node) is not Select:
        raise _StructureShapeRefusal("sequence_origin")
    return _structure_dml_shape(trace, owner, context)


def _structure_sql_shape(trace, owner: _StructureInvocation, context) -> StructureSqlShape | None:
    """Freeze only value-free current invocation structure before listeners."""
    statement = context.statement
    executemany = context.executemany
    if type(executemany) is not bool:
        raise ValueError("unknown current execution mode")
    options = context.execution_options
    if type(options) not in (dict, immutabledict) or any(
        type(key) is not str or key != _STRUCTURE_WRITE_OPTION or type(options[key]) is not bool for key in options
    ):
        raise ValueError("unsupported execution options")
    if owner.role == "driver":
        if owner.driver_role is None or id(statement) != owner.statement or context.compiled is not None:
            _structure_refuse(trace, "fixed_driver_invocation_changed")
            return
        shape = StructureSqlShape("fixed_driver", (owner.driver_role,), (), executemany)
    else:
        invoked = context.invoked_statement
        if owner.store_role == "snapshot_sequence":
            return _structure_snapshot_sequence_shape(trace, owner, context, invoked)
        if type(invoked) in (Insert, Update):
            return _structure_dml_shape(trace, owner, context)
        if id(invoked) != owner.statement or type(invoked) is not Select:
            _structure_refuse(trace, "current_statement_unbound_or_literal")
            return
        compiled = context.compiled
        expected_compiler = SQLiteCompiler if type(context) is SQLiteExecutionContext else PGCompiler_psycopg
        if (
            type(compiled) is not expected_compiler
            or compiled.literal_execute_params
            or "construct_params" in compiled.__dict__
            or any(name in compiled.__dict__ for name in _STRUCTURE_VISITOR_NAMES)
        ):
            _structure_refuse(trace, "literal_execute_or_missing_compiler")
            return
        # The current execution's extracted bind objects pair with the
        # compiler's actual cache-key bind positions. Never read cached
        # compiled.params, generate a cache key, or compile another SQL.
        extracted = context.extracted_parameters
        if extracted is None:
            pairs = tuple((bind, bind) for bind in compiled.bind_names)
        else:
            if compiled.cache_key is None or len(extracted) != len(compiled.cache_key.bindparams):
                _structure_refuse(trace, "current_bind_provenance_missing")
                return
            pairs = tuple(zip(extracted, compiled.cache_key.bindparams, strict=True))
        if any(
            type(current) is not BindParameter
            or type(cached) is not BindParameter
            or current.expanding is not cached.expanding
            or current.literal_execute
            or cached.literal_execute
            for current, cached in pairs
        ):
            _structure_refuse(trace, "current_bind_source_changed")
            return
        rows = context.compiled_parameters
        if type(rows) is not list or not rows or any(type(row) is not dict for row in rows):
            _structure_refuse(trace, "current_parameter_shape_unknown")
            return
        names = {id(current): compiled.bind_names[cached] for current, cached in pairs}
        binding_roles: dict[int, int] = {}
        arities = []

        def table_role(table):
            role = next((index for index, known in enumerate(_STRUCTURE_TABLES) if table is known), None)
            # SQLAlchemy Column.__eq__ creates SQL: identity only.
            if (
                role is None
                or len(tuple(table.columns)) != len(_STRUCTURE_COLUMNS[role])
                or any(current is not known for current, known in zip(table.columns, _STRUCTURE_COLUMNS[role], strict=True))
            ):
                raise ValueError("unsupported owned table")
            return role

        def expression(node, depth=0):
            if depth >= 64:
                raise ValueError("unsupported structural depth")
            node_type = type(node)
            if node_type is Column:
                role = table_role(node.table)
                column = next((index for index, known in enumerate(_STRUCTURE_COLUMNS[role]) if node is known), None)
                if column is None:
                    raise ValueError("unsupported owned column")
                return ("column", role, column)
            if node_type is Table:
                return ("table", table_role(node))
            if node_type is BindParameter:
                identity = id(node)
                if identity not in names or node.literal_execute:
                    raise ValueError("unbound current parameter")
                if identity not in binding_roles:
                    name = names[identity]
                    expanded = context._expanded_parameters.get(name) if node.expanding else None
                    if node.expanding:
                        if type(expanded) is not list or any(
                            type(key) is not str or any(key not in row for row in rows) for key in expanded
                        ):
                            raise ValueError("unbound current expansion")
                        arity = len(expanded)
                    else:
                        if any(name not in row for row in rows):
                            raise ValueError("missing current bind")
                        arity = 1
                    binding_roles[identity] = len(arities)
                    arities.append(arity)
                return ("bind", binding_roles[identity], node.expanding, arities[binding_roles[identity]])
            if node_type is BinaryExpression:
                operator = next((index for index, known in enumerate(_STRUCTURE_OPERATORS) if node.operator is known), None)
                if operator is None or node.modifiers:
                    raise ValueError("unsupported binary operator")
                return ("binary", operator, expression(node.left, depth + 1), expression(node.right, depth + 1))
            if node_type is BooleanClauseList:
                operator = next((index for index, known in enumerate(_STRUCTURE_OPERATORS) if node.operator is known), None)
                if operator is None:
                    raise ValueError("unsupported boolean operator")
                return ("boolean", operator, tuple(expression(clause, depth + 1) for clause in node.clauses))
            if node_type is Grouping:
                return ("group", expression(node.element, depth + 1))
            if node_type is UnaryExpression:
                modifier = next((index for index, known in enumerate(_STRUCTURE_OPERATORS) if node.modifier is known), None)
                if node.operator is not None or modifier is None:
                    raise ValueError("unsupported unary operator")
                return ("unary", modifier, expression(node.element, depth + 1))
            if node_type in (Null, True_, False_):
                return ("null" if node_type is Null else "true" if node_type is True_ else "false",)
            raise ValueError("unsupported SQL expression")

        if type(invoked) is Select:
            if (
                invoked._prefixes
                or invoked._suffixes
                or invoked._hints
                or invoked._statement_hints
                or invoked._distinct
                or invoked._for_update_arg is not None
                or invoked._group_by_clauses
                or invoked._having_criteria
                or invoked._setup_joins
                or invoked._independent_ctes
                or invoked._correlate
                or invoked._correlate_except is not None
                or invoked._fetch_clause is not None
                or invoked._with_options
            ):
                raise ValueError("unsupported select clauses")
            tree = (
                tuple(expression(column) for column in invoked._raw_columns),
                tuple(expression(table) for table in invoked._from_obj),
                tuple(expression(clause) for clause in invoked._where_criteria),
                tuple(expression(clause) for clause in invoked._order_by_clauses),
                None if invoked._limit_clause is None else expression(invoked._limit_clause),
                None if invoked._offset_clause is None else expression(invoked._offset_clause),
            )
            kind = "select"
        if set(binding_roles) != set(names):
            raise ValueError("current bind omitted from closed grammar")
        shape = StructureSqlShape(kind, tree, (), executemany)
    return shape


def _structure_emit(trace, thread: _StructureThread, kind: str, *, owner=None, source: str, **fields) -> StructureEvent:
    clock = next((thread.frames[key] for key in reversed(thread.clocks) if key in thread.frames), None)
    phase = clock.phase if clock is not None else thread.worker_phase
    clock_id = clock.event if clock is not None else thread.worker_clock
    memo = _CATALOG_STAGE_CONTEXT.get()
    stage = next((index for index, known in enumerate(trace.memos, 1) if memo is known), None)
    if memo is not None and (type(memo) is not RetentionStageMemo or stage is None or memo._closed is not False):
        _structure_refuse(trace, "unknown_or_closed_carrier")
    expected = thread.expected
    expected_stage = next((index for index, known in enumerate(trace.memos, 1) if id(known) == expected), None)
    if owner is not None:
        phase, clock_id = owner.phase, owner.clock
        if owner.memo is not None and thread.submission is None:
            expected = owner.memo
            expected_stage = owner.stage
    if kind in ("sql_attempt", "db_clock_attempt", "admission_attempt", "window_attempt"):
        if phase == "retention" and (memo is None or (expected is not None and id(memo) != expected)):
            _structure_refuse(trace, "missing_or_wrong_carrier")
        if phase in ("physical", "guard", "quota") and memo is not None:
            _structure_refuse(trace, "physical_work_borrowed_carrier")
        if thread.submission is not None and clock_id != thread.worker_clock:
            _structure_refuse(trace, "worker_clock_owner_changed")
    event = StructureEvent(
        _structure_number(thread),
        kind,
        phase,
        clock_id,
        stage,
        expected_stage,
        thread.submission,
        thread.identity,
        None,
        None,
        None,
        source,
    )
    event = replace(event, **fields)
    thread.events.append(event)
    owner_key = ("submission", event.submission) if event.submission is not None else ("clock", event.clock or 0)
    previous = thread.last.get(owner_key)
    if previous is not None:
        thread.edges.append(StructureEdge(previous, event.ordinal, "owner_order"))
    thread.last[owner_key] = event.ordinal
    container = event.window if event.window is not None else event.clock
    if container is not None and container != event.ordinal:
        thread.edges.append(StructureEdge(container, event.ordinal, "contains"))
    return event


def _encoded_len_sites(code: CodeType, expected: int) -> frozenset[int]:
    """Select original len(....encode(...)) expressions, including CALL_KW."""
    instructions = tuple(dis.get_instructions(code))
    sites = []
    for index, load in enumerate(instructions):
        if load.opname != "LOAD_GLOBAL" or load.argval != "len":
            continue
        for end in range(index + 1, len(instructions)):
            call = instructions[end]
            if call.opname == "CALL" and (call.positions.lineno, call.positions.col_offset) == (
                load.positions.lineno,
                load.positions.col_offset,
            ):
                if any(item.opname == "LOAD_ATTR" and item.argval == "encode" for item in instructions[index + 1 : end]):
                    assert call.arg == 1, "byte observer original len signature changed"
                    sites.append(call.offset)
                break
    assert len(sites) == expected, "byte observer original encoded len sites changed"
    return frozenset(sites)


def _keyword_call_names(code: CodeType, instructions: tuple[dis.Instruction, ...], index: int, names: tuple[str, ...]) -> bool:
    """Recognize only the two native keyword grammars with their exact tuple."""
    call, previous = instructions[index], instructions[index - 1]
    if call.opname == "CALL_KW":
        return previous.opname == "LOAD_CONST" and type(previous.argval) is tuple and previous.argval == names
    if call.opname == "CALL" and previous.opname == "KW_NAMES" and type(previous.arg) is int:
        return code.co_consts[previous.arg] == names
    return False


def _scanner_size_sites(code: CodeType, variables: tuple[str, str]) -> dict[int, str]:
    """Bind the four original bounded-size keyword expressions, not other calls."""
    instructions = tuple(dis.get_instructions(code))
    loads = [(index, item) for index, item in enumerate(instructions) if item.opname == "LOAD_GLOBAL" and item.argval == "utf8_size"]
    assert len(loads) == len(variables), "byte observer original bounded scanner sites changed"
    sites = {}
    for (index, load), variable in zip(loads, variables, strict=True):
        call_index = next(
            end
            for end in range(index + 1, len(instructions))
            if instructions[end].opname in ("CALL_KW", "CALL")
            and (instructions[end].positions.lineno, instructions[end].positions.col_offset)
            == (load.positions.lineno, load.positions.col_offset)
        )
        call = instructions[call_index]
        assert call.arg == 3 and _keyword_call_names(code, instructions, call_index, ("errors", "limit")), (
            "byte observer original bounded scanner signature changed"
        )
        assert instructions[index + 1].argval == variable, "byte observer original bounded scanner input changed"
        sites[call.offset] = variable
    return sites


def _scanner_size_windows(code: CodeType, sites: dict[int, str]) -> dict[int, tuple[range, int]]:
    """Include only an admitted CALL and its own following inline caches."""
    instructions = tuple(dis.get_instructions(code, show_caches=True))
    windows = {}
    for index, call in enumerate(instructions):
        if call.offset not in sites:
            continue
        assert call.opname in ("CALL", "CALL_KW") and call.positions.lineno is not None, "bounded scanner original call window changed"
        end = index + 1
        while end < len(instructions) and instructions[end].opname == "CACHE":
            end += 1
        assert end < len(instructions), "bounded scanner original call window has no successor"
        windows[call.offset] = (range(call.offset, instructions[end].offset), call.positions.lineno)
    assert windows.keys() == sites.keys(), "bounded scanner original call windows are incomplete"
    return windows


_SCANNER_SIZE_SITES = {_FIND: _scanner_size_sites(_FIND, ("value", "key")), _REFERENCE: _scanner_size_sites(_REFERENCE, ("ref", "value"))}
_SCANNER_SIZE_WINDOWS = {code: _scanner_size_windows(code, sites) for code, sites in _SCANNER_SIZE_SITES.items()}
_EXTENT_SITES = {_UTF8_CODE: _encoded_len_sites(_UTF8_CODE, 1)}
_PREPARATION_SITES = {_POLICY_NAMES: _encoded_len_sites(_POLICY_NAMES, 1), _TOKEN: _encoded_len_sites(_TOKEN, 1)}
_BYTE_CODES = (*_SCANNER_SIZE_SITES, *_EXTENT_SITES, *_PREPARATION_SITES, _CHUNK_REQUEST)
_UTF8_RETURN_BRANCHES = {
    item.offset: {13: "length_excess", 15: "ascii", 21: "encoded_excess", 22: "encoded"}[item.positions.lineno - _UTF8_CODE.co_firstlineno]
    for item in dis.get_instructions(_UTF8_CODE)
    if item.opname in ("RETURN_VALUE", "RETURN_CONST") and item.positions.lineno is not None
}
assert set(_UTF8_RETURN_BRANCHES.values()) == {"length_excess", "ascii", "encoded_excess", "encoded"}, (
    "byte observer original bounded scanner branches changed"
)


def _scanner_sources_current() -> bool:
    return (
        credential_material._find is _FIND_FUNCTION
        and _FIND_FUNCTION.__code__ is _FIND
        and credential_material._allowed_reference_finding is _REFERENCE_FUNCTION
        and _REFERENCE_FUNCTION.__code__ is _REFERENCE
        and credential_material.utf8_size is _UTF8_SIZE
        and bounded_serialization.utf8_size is _UTF8_SIZE
        and _UTF8_SIZE.__code__ is _UTF8_CODE
        and _UTF8_SIZE.__globals__ is bounded_serialization.__dict__
        and bounded_serialization.__dict__.get("str", str) is str
        and bounded_serialization.__dict__.get("len", len) is len
        and bounded_serialization.__dict__.get("range", range) is range
        and bounded_serialization.__dict__.get("slice", slice) is slice
        and bounded_serialization._CHUNK_CHARACTERS == _UTF8_CHUNK_CHARACTERS == 4096
        and credential_material._SECRET_REFERENCE_NAME_MAX_BYTES == _REFERENCE_MAX_BYTES == 256
    )


def _private_size_sources_current() -> bool:
    return (
        _scanner_sources_current()
        and continuation_selection.load_private_chunks is _LOAD_CHUNKS_FUNCTION
        and _LOAD_CHUNKS_FUNCTION.__code__ is _LOAD_CHUNKS
        and continuation_selection.nested_private_chunks is _NESTED_CHUNKS_FUNCTION
        and _NESTED_CHUNKS_FUNCTION.__code__ is _NESTED_CHUNKS
        and continuation_selection.utf8_size is _UTF8_SIZE
        and continuation_selection.verify_private_chunk is _CHUNK_PUBLIC
        and _CHUNK_PUBLIC.__code__ is _CHUNK_REQUEST
        and continuation_selection.read_private_chunk is _READ_PRIVATE_CHUNK
        and stored_continuation.read_private_chunk is _READ_PRIVATE_CHUNK
        and _READ_PRIVATE_CHUNK.__code__ is _READ_PRIVATE_CHUNK_CODE
        and stored_continuation.sha256 is _SHA256
        and continuation_selection.CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES == _PRIVATE_UTF8_LIMIT
    )


@dataclass(frozen=True, slots=True)
class NativeByteLimit:
    channel: str
    min_calls: int
    max_calls: int
    max_bytes: int


@dataclass(frozen=True, slots=True)
class NativeByteBudget:
    """Explicit finite caps for one complete DATA key or closed validator row."""

    stage: int
    family: str
    limits: tuple[NativeByteLimit, ...]
    key: object | None = None
    producer: FunctionType | None = None
    invocation: int | None = None


@dataclass(frozen=True, slots=True)
class NativeByteTotals:
    stage: int
    family: str
    group: int
    channel: str
    calls: int
    bytes: int
    invocation: int | None = None


@dataclass(slots=True)
class ContextCanonicalInspection:
    """One complete original capture, distinct from its scalar validators."""

    memo: RetentionStageMemo
    invocation: int
    thread_id: int
    expected_memo_id: int | None
    exited: bool = False
    byte_calls: Counter[str] = field(default_factory=Counter)
    byte_counts: Counter[str] = field(default_factory=Counter)


class _LifetimeProfileUnsupported(RuntimeError):
    """The lifetime observer cannot safely exchange callbacks under this chain."""


class _StructureLifetimeMonitor:
    """Own only six original lifetimes; retain no execution operands/frames."""

    def __init__(self, trace) -> None:
        self.trace = trace
        self.tool: int | None = None
        self.name = "retention-structure-lifetimes-" + str(id(self))
        self.callbacks: list[list[object]] = []
        self.locals: list[tuple[CodeType, int]] = []
        self.restored_locals: set[CodeType] = set()
        self.uncertain_callback: int | None = None
        self.uncertain_local: CodeType | None = None
        self.global_expected = 0
        self.global_uncertain = False
        self.installing = True
        self.armed = False
        self.closed = False
        self.unsupported_prior = False
        self.exchange_uncertain = False
        self.local_mask = _LIFETIME_YIELD | _LIFETIME_RETURN
        self.global_mask = _LIFETIME_UNWIND
        self.deliveries = tuple(
            (event, self._callback(kind))
            for event, kind in (
                (_LIFETIME_YIELD, "yield"),
                (_LIFETIME_RETURN, "returned"),
                (_LIFETIME_UNWIND, "unwound"),
            )
        )

    def _callback(self, kind: str):
        trace = self.trace

        def deliver(code, offset, value):
            entry = None
            thread = None
            try:
                if not any(code is known for known in _STRUCTURE_LIFETIME_CODES):
                    if kind != "unwound":
                        _structure_refuse(trace, "lifetime_unowned_local_delivery")
                    return
                # This audited acquisition and ALL entry bookkeeping share the
                # primary-preserving boundary, including PY_UNWIND acquisition.
                frame = _LIFETIME_FRAME(1)
                identity = get_ident()
                thread = trace._structure_threads.get(identity)
                if thread is not None:
                    pending = thread.pending_returns.get(id(frame))
                    owner = thread.frames.get(id(frame))
                    if pending is not None and owner is not None:
                        if thread.native_entry is not None:
                            _structure_refuse(trace, "lifetime_native_entry_reentered")
                            return
                        entry = _StructureNativeEntry(
                            kind,
                            id(frame),
                            id(code),
                            id(frame.f_globals),
                            offset,
                            id(owner),
                            pending.generation,
                            owner.event,
                            identity,
                            id(deliver),
                        )
                        thread.native_entry = entry
                try:
                    trace._structure_lifetime_event(frame, kind, code, offset, value)
                finally:
                    if entry is not None:
                        if thread.native_entry is not entry:
                            _structure_refuse(trace, "lifetime_native_entry_changed")
                        thread.native_entry = None
            except BaseException as observer:
                trace._structure_observer_incomplete = True
                # This is our native observer domain, not a failed prior hook.
                # Its monotonic structure refusal already prevents qualification.
                with suppress(BaseException):
                    _structure_refuse(
                        trace, "lifetime_callback_failed" if isinstance(observer, Exception) else "lifetime_callback_interrupted"
                    )
                if kind != "unwound" and not isinstance(observer, Exception):
                    raise
            finally:
                if entry is not None and thread is not None:
                    # No active receipt, frame or native operand outlives this
                    # original registered callback, even on an interruption.
                    thread.native_entry = None

        return deliver

    def _before_mutation(self, kind: str, target: object) -> None:
        """Opposed controls interrupt BEFORE an actual owned API mutation."""

    def _owned(self) -> bool:
        if self.tool is None:
            return False
        name = _LIFETIME_GET_TOOL(self.tool)
        return type(name) is str and name == self.name

    def _masks_current(self) -> bool:
        if self.tool is None:
            return False
        for code in _STRUCTURE_LIFETIME_CODES:
            prior = next((previous for known, previous in self.locals if known is code), None)
            if prior is None:
                allowed = (0,)
            elif self.uncertain_local is code:
                allowed = (prior, self.local_mask)
            elif code in self.restored_locals:
                allowed = (prior,)
            else:
                allowed = (self.local_mask,)
            if _LIFETIME_GET_LOCAL(self.tool, code) not in allowed:
                return False
        return True

    def _callbacks_current(self) -> bool:
        if self.exchange_uncertain:
            raise RuntimeError("lifetime callback exchange completion is uncertain")
        if not self.trace._structure_exchange_supported():
            raise _LifetimeProfileUnsupported("lifetime callback exchanges require an original owned hook without a prior profiler")
        if not self._owned():
            return False
        for row in self.callbacks:
            event, previous, installed = row
            intended = next(callback for known, callback in self.deliveries if known == event)
            # Rollback needs the actually received displaced identity. The
            # supported chain excludes arbitrary prior hooks; an unreceived
            # exchange stays uncertain, with no universal atomicity claim.
            if not self.trace._structure_exchange_supported():
                raise _LifetimeProfileUnsupported("lifetime profile chain changed before callback exchange")
            self.exchange_uncertain = True
            displaced = _LIFETIME_REGISTER(self.tool, event, installed)
            self.exchange_uncertain = False
            allowed = (installed, intended, previous) if self.uncertain_callback == event else (installed,)
            if not any(displaced is candidate for candidate in allowed):
                if not self.trace._structure_exchange_supported():
                    raise _LifetimeProfileUnsupported("lifetime profile chain changed before foreign callback restoration")
                self.exchange_uncertain = True
                _LIFETIME_REGISTER(self.tool, event, displaced)
                self.exchange_uncertain = False
                return False
            if self.uncertain_callback == event:
                self.uncertain_callback = None
        return self._owned()

    def install(self) -> None:
        if not self.trace._structure_exchange_supported():
            self.unsupported_prior = True
            self.installing = False
            self.closed = True
            _structure_refuse(self.trace, "lifetime_prior_profile_unsupported")
            return
        try:
            if sys.monitoring is not _LIFETIME_NAMESPACE or any(
                _LIFETIME_NAMESPACE.__dict__.get(name) is not function for name, function in _STRUCTURE_MONITOR_API
            ):
                raise RuntimeError("lifetime observer requires the original monitoring API")
            available = [
                tool
                for tool in (4, 3)
                if _LIFETIME_GET_TOOL(tool) is None
                and _LIFETIME_GET_EVENTS(tool) == 0
                and all(_LIFETIME_GET_LOCAL(tool, code) == 0 for code in _STRUCTURE_LIFETIME_CODES)
            ]
            if not available:
                raise RuntimeError("lifetime observer refuses occupied tools or masks")
            tool = available[0]
            self._before_mutation("claim", tool)
            if (
                _LIFETIME_GET_TOOL(tool) is not None
                or _LIFETIME_GET_EVENTS(tool) != 0
                or any(_LIFETIME_GET_LOCAL(tool, code) != 0 for code in _STRUCTURE_LIFETIME_CODES)
            ):
                raise RuntimeError("lifetime slot changed before claim")
            _LIFETIME_USE_TOOL(tool, self.name)
            self.tool = tool
            for event, _callback in self.deliveries:
                self._before_mutation("probe", event)
                if not self._owned() or _LIFETIME_GET_EVENTS(tool) != 0 or not self._masks_current():
                    raise RuntimeError("lifetime ownership changed before callback probe")
                if not self.trace._structure_exchange_supported():
                    raise _LifetimeProfileUnsupported("lifetime profile chain changed before initial callback probe")
                self.uncertain_callback = event
                self.exchange_uncertain = True
                previous = _LIFETIME_REGISTER(tool, event, None)
                self.exchange_uncertain = False
                self.callbacks.append([event, previous, None])
                self.uncertain_callback = None
            if any(previous is not None for _event, previous, _installed in self.callbacks):
                raise RuntimeError("lifetime observer refuses retained lifetime callbacks")
            for row, (_event, callback) in zip(self.callbacks, self.deliveries, strict=True):
                event = row[0]
                self._before_mutation("callback", event)
                if not self._owned() or _LIFETIME_GET_EVENTS(tool) != 0 or not self._callbacks_current() or not self._masks_current():
                    raise RuntimeError("lifetime ownership changed before callback install")
                if not self.trace._structure_exchange_supported():
                    raise _LifetimeProfileUnsupported("lifetime profile chain changed before callback installation")
                self.uncertain_callback = event
                self.exchange_uncertain = True
                previous = _LIFETIME_REGISTER(tool, event, callback)
                self.exchange_uncertain = False
                row[2] = callback
                self.uncertain_callback = None
                if previous is not None:
                    if not self.trace._structure_exchange_supported():
                        raise _LifetimeProfileUnsupported("lifetime profile chain changed before acquired callback restoration")
                    self.exchange_uncertain = True
                    _LIFETIME_REGISTER(tool, event, previous)
                    self.exchange_uncertain = False
                    raise RuntimeError("lifetime callback changed during acquisition")
            for code in _STRUCTURE_LIFETIME_CODES:
                self._before_mutation("local", code)
                if not self._callbacks_current() or not self._owned() or _LIFETIME_GET_EVENTS(tool) != 0 or not self._masks_current():
                    raise RuntimeError("lifetime ownership changed before local install")
                previous = _LIFETIME_GET_LOCAL(tool, code)
                if previous != 0:
                    raise RuntimeError("lifetime local mask changed during acquisition")
                self.locals.append((code, previous))
                self.uncertain_local = code
                _LIFETIME_SET_LOCAL(tool, code, self.local_mask)
                self.uncertain_local = None
            self._before_mutation("global", tool)
            if (
                not self._callbacks_current()
                or not self._owned()
                or _LIFETIME_GET_EVENTS(tool) != 0
                or any(_LIFETIME_GET_LOCAL(tool, code) != self.local_mask for code in _STRUCTURE_LIFETIME_CODES)
            ):
                raise RuntimeError("lifetime ownership changed before global install")
            self.global_uncertain = True
            _LIFETIME_SET_EVENTS(tool, self.global_mask)
            self.global_expected = self.global_mask
            self.global_uncertain = False
            self.armed = True
        except _LifetimeProfileUnsupported:
            self.installing = False
            self.unsupported_prior = True
            _structure_refuse(self.trace, "lifetime_prior_profile_unsupported")
            self.close()
            return
        except BaseException as primary:
            self.installing = False
            self.close(primary)
            raise
        self.installing = False

    def current(self) -> bool:
        return (
            self.armed
            and not self.closed
            and not self.exchange_uncertain
            and self.tool is not None
            and sys.monitoring is _LIFETIME_NAMESPACE
            and all(_LIFETIME_NAMESPACE.__dict__.get(name) is function for name, function in _STRUCTURE_MONITOR_API)
            and self._owned()
            and _LIFETIME_GET_EVENTS(self.tool) == self.global_mask
            and all(_LIFETIME_GET_LOCAL(self.tool, code) == self.local_mask for code in _STRUCTURE_LIFETIME_CODES)
        )

    def close(self, primary: BaseException | None = None) -> None:
        try:
            self._restore()
        except _LifetimeProfileUnsupported:
            self.unsupported_prior = True
            with suppress(BaseException):
                _structure_refuse(self.trace, "lifetime_prior_profile_unsupported")
        except BaseException:
            self.trace._structure_observer_incomplete = True
            # Preserve actual prior-hook failure attribution separately from
            # our own lifetime cleanup error while refusing structure coverage.
            with suppress(BaseException):
                _structure_refuse(self.trace, "lifetime_monitor_cleanup_incomplete")
            if primary is None:
                raise

    def _boundary(self, kind: str, target: object) -> BaseException | None:
        try:
            self._before_mutation(kind, target)
        except BaseException as error:
            return error
        return None

    def _restore(self) -> None:
        if self.closed:
            return
        tool = self.tool
        if tool is None:
            self.closed = True
            self.armed = False
            return
        if self.exchange_uncertain:
            raise RuntimeError("lifetime callback exchange completion is uncertain")
        if not self.trace._structure_exchange_supported():
            raise _LifetimeProfileUnsupported("lifetime callback exchanges require an original owned hook without a prior profiler")
        errors = []
        try:
            owned = self._owned()
            if not owned:
                errors.append(RuntimeError("lifetime observer lost its tool"))
            elif self.uncertain_callback is not None and not any(row[0] == self.uncertain_callback for row in self.callbacks):
                owned = False
                errors.append(RuntimeError("lifetime callback acquisition is uncertain"))
            elif not self._callbacks_current():
                owned = False
                errors.append(RuntimeError("lifetime callback changed without ownership"))
            if owned:
                failure = self._boundary("close-global", tool)
                if failure is not None:
                    errors.append(failure)
                allowed_global = (0, self.global_mask) if self.global_uncertain else (self.global_expected,)
                if (
                    not self._callbacks_current()
                    or not self._owned()
                    or _LIFETIME_GET_EVENTS(tool) not in allowed_global
                    or not self._masks_current()
                ):
                    owned = False
                    errors.append(RuntimeError("lifetime ownership changed before global cleanup"))
                else:
                    try:
                        self.global_uncertain = True
                        _LIFETIME_SET_EVENTS(tool, 0)
                        self.global_expected = 0
                        self.global_uncertain = False
                    except BaseException as error:
                        errors.append(error)
            for code, previous in reversed(self.locals):
                if not owned:
                    break
                failure = self._boundary("close-local", code)
                if failure is not None:
                    errors.append(failure)
                allowed = (
                    (previous, self.local_mask)
                    if self.uncertain_local is code
                    else ((previous,) if code in self.restored_locals else (self.local_mask,))
                )
                if (
                    not self._callbacks_current()
                    or not self._owned()
                    or _LIFETIME_GET_EVENTS(tool) != self.global_expected
                    or _LIFETIME_GET_LOCAL(tool, code) not in allowed
                    or not self._masks_current()
                ):
                    owned = False
                    errors.append(RuntimeError("lifetime ownership changed before local cleanup"))
                    break
                try:
                    self.uncertain_local = code
                    _LIFETIME_SET_LOCAL(tool, code, previous)
                    self.restored_locals.add(code)
                    self.uncertain_local = None
                except BaseException as error:
                    errors.append(error)
            for row in reversed(self.callbacks):
                if not owned:
                    break
                event, previous, installed = row
                failure = self._boundary("close-callback", event)
                if failure is not None:
                    errors.append(failure)
                if (
                    not self._callbacks_current()
                    or not self._owned()
                    or _LIFETIME_GET_EVENTS(tool) != self.global_expected
                    or not self._masks_current()
                ):
                    owned = False
                    errors.append(RuntimeError("lifetime ownership changed before callback cleanup"))
                    break
                try:
                    if not self.trace._structure_exchange_supported():
                        raise _LifetimeProfileUnsupported("lifetime profile chain changed before cleanup callback exchange")
                    self.uncertain_callback = event
                    self.exchange_uncertain = True
                    displaced = _LIFETIME_REGISTER(tool, event, previous)
                    self.exchange_uncertain = False
                    if displaced is not installed:
                        if not self.trace._structure_exchange_supported():
                            raise _LifetimeProfileUnsupported("lifetime profile chain changed before displaced callback restoration")
                        self.exchange_uncertain = True
                        _LIFETIME_REGISTER(tool, event, displaced)
                        self.exchange_uncertain = False
                        self.uncertain_callback = None
                        owned = False
                        raise RuntimeError("lifetime callback changed during cleanup")
                    row[2] = previous
                    self.uncertain_callback = None
                except _LifetimeProfileUnsupported:
                    raise
                except BaseException as error:
                    errors.append(error)
            if owned:
                failure = self._boundary("free", tool)
                if failure is not None:
                    errors.append(failure)
                restored = (
                    self._callbacks_current()
                    and self._owned()
                    and _LIFETIME_GET_EVENTS(tool) == 0
                    and all(_LIFETIME_GET_LOCAL(tool, code) == 0 for code in _STRUCTURE_LIFETIME_CODES)
                )
                if not restored:
                    errors.append(RuntimeError("lifetime ownership changed before free"))
                else:
                    try:
                        _LIFETIME_FREE_TOOL(tool)
                        self.closed = True
                        self.armed = False
                    except BaseException as error:
                        errors.append(error)
        except _LifetimeProfileUnsupported:
            # A boundary may install an unsupported prior hook AND raise an
            # original interruption. Stop exchanges, but do not discard that
            # already captured error in favor of a capability-only refusal.
            self.unsupported_prior = True
            with suppress(BaseException):
                _structure_refuse(self.trace, "lifetime_prior_profile_unsupported")
            if not errors:
                raise
        if errors:
            raise errors[0]


class _ByteMonitor:
    """Own a reversible CALL family; an unowned tool is not an empty tool."""

    def __init__(self, callback) -> None:
        self.tool: int | None = None
        self.name = "retention-native-bytes-" + str(id(self))
        self.callbacks: list[tuple[int, object, object]] = []
        self.locals: list[tuple[CodeType, int]] = []
        self.uncertain_callbacks: set[int] = set()
        self.uncertain_locals: set[CodeType] = set()
        self.closed = False
        assert type(_REGISTER_CALLBACK) is BuiltinFunctionType, "byte observer requires the native monitoring API"
        available = [
            tool
            for tool in (3, 4)
            if sys.monitoring.get_tool(tool) is None
            and sys.monitoring.get_events(tool) == 0
            and all(sys.monitoring.get_local_events(tool, code) == 0 for code in _BYTE_CODES)
        ]
        if not available:
            raise RuntimeError("byte observer refuses occupied monitoring tools or masks")
        tool = available[0]
        primary = None
        try:
            sys.monitoring.use_tool_id(tool, self.name)
            self.tool = tool
            previous = [
                self._replace(event, None)
                for event in (sys.monitoring.events.CALL, sys.monitoring.events.C_RETURN, sys.monitoring.events.C_RAISE)
            ]
            if any(item is not None for item in previous):
                raise RuntimeError("byte observer refuses retained CALL-family callbacks")
            assert self._replace(sys.monitoring.events.CALL, callback) is None
            for code in _BYTE_CODES:
                self._before_mutation("local", code)
                previous_mask = sys.monitoring.get_local_events(tool, code)
                assert previous_mask == 0, "byte observer local mask changed before installation"
                self.uncertain_locals.add(code)
                sys.monitoring.set_local_events(tool, code, sys.monitoring.events.CALL)
                self.locals.append((code, previous_mask))
                self.uncertain_locals.remove(code)
        except BaseException as error:
            primary = error
        if primary is not None:
            self.close(primary)
            raise primary

    def _before_mutation(self, kind: str, target: object) -> None:
        """A test seam before, never around, a native API mutation."""

    def _replace(self, event: int, callback):
        assert self.tool is not None
        self._before_mutation("callback", event)
        self.uncertain_callbacks.add(event)
        previous = _REGISTER_CALLBACK(self.tool, event, callback)
        self.callbacks.append((event, previous, callback))
        self.uncertain_callbacks.remove(event)
        return previous

    def close(self, primary: BaseException | None = None) -> None:
        if self.closed:
            return
        errors = []
        tool = self.tool
        if tool is None:
            self.closed = True
            return
        if sys.monitoring.get_tool(tool) != self.name:
            errors.append(RuntimeError("byte observer lost its monitoring tool"))
        else:
            if self.uncertain_callbacks or self.uncertain_locals:
                errors.append(RuntimeError("byte observer mutation completion is uncertain"))
            if sys.monitoring.get_events(tool) != 0:
                errors.append(RuntimeError("byte observer global mask changed without ownership"))
            for code, previous in reversed(self.locals):
                try:
                    if sys.monitoring.get_local_events(tool, code) != sys.monitoring.events.CALL:
                        raise RuntimeError("byte observer local mask changed without ownership")
                    sys.monitoring.set_local_events(tool, code, previous)
                except BaseException as error:
                    errors.append(error)
            unsafe = set(self.uncertain_callbacks)
            for event, previous, installed in reversed(self.callbacks):
                if event in unsafe:
                    continue
                try:
                    displaced = _REGISTER_CALLBACK(tool, event, previous)
                    if displaced is not installed:
                        _REGISTER_CALLBACK(tool, event, displaced)
                        raise RuntimeError("byte observer callback changed without ownership")
                except BaseException as error:
                    unsafe.add(event)
                    errors.append(error)
            if not errors:
                try:
                    sys.monitoring.free_tool_id(tool)
                except BaseException as error:
                    errors.append(error)
        if errors:
            # Do not advertise an uncertain slot as free or erase another owner.
            raise BaseExceptionGroup("byte observer cleanup incomplete", ([primary] if primary is not None else []) + errors)
        self.closed = True


def _direct_call_site(
    code: CodeType,
    symbol: str,
    *,
    loads_expected: int = 1,
    occurrence: int = 0,
    arguments: int,
    load_op: str = "LOAD_GLOBAL",
    keywords: tuple[str, ...] | None = None,
) -> tuple[range, int]:
    """Bind one closed original callsite, including interpreter inline caches."""
    instructions = tuple(dis.get_instructions(code))
    loads = [index for index, instruction in enumerate(instructions) if instruction.opname == load_op and instruction.argval == symbol]
    assert len(loads) == loads_expected, "uncached validation original callsite changed"
    index = next(
        index
        for index in range(loads[occurrence] + 1, len(instructions))
        if instructions[index].opname in (("CALL",) if keywords is None else ("CALL_KW", "CALL"))
    )
    call = instructions[index]
    assert call.arg == arguments and call.positions.lineno is not None, "uncached validation original signature changed"
    if keywords is not None:
        assert _keyword_call_names(code, instructions, index, keywords), "uncached validation original keyword signature changed"
    return range(call.offset, instructions[index + 1].offset), call.positions.lineno


def _structure_row_callback_site() -> tuple[frozenset[int], int, int]:
    """One original six-argument listener CALL and its two-local assignment."""
    code = _STRUCTURE_SINGLE_CONTEXT.__code__
    instructions = tuple(dis.get_instructions(code))
    expected = (
        ("LOAD_FAST", "fn"),
        ("LOAD_FAST", "self"),
        ("LOAD_FAST", "cursor"),
        ("LOAD_FAST", "str_statement"),
        ("LOAD_FAST", "effective_parameters"),
        ("LOAD_FAST", "context"),
        ("LOAD_FAST", "context"),
        ("LOAD_ATTR", "executemany"),
    )
    sites = []
    for index, call in enumerate(instructions):
        if call.opname != "CALL" or call.arg != 6 or instructions[index + 1].opname != "UNPACK_SEQUENCE":
            continue
        unpack = instructions[index + 1]
        if unpack.arg != 2:
            continue
        stores = instructions[index + 2 : index + 4]
        if stores[0].opname == "STORE_FAST_STORE_FAST":
            names = stores[0].argval
        else:
            names = tuple(item.argval for item in stores if item.opname == "STORE_FAST")
        if names != ("str_statement", "effective_parameters"):
            continue
        for start in range(max(0, index - 12), index):
            loads = []
            for item in instructions[start:index]:
                if item.opname == "PUSH_NULL":
                    continue
                if item.opname == "LOAD_FAST_LOAD_FAST":
                    loads.extend(("LOAD_FAST", name) for name in item.argval)
                else:
                    loads.append((item.opname, item.argval))
            if tuple(loads) == expected:
                sites.append((call.offset, unpack.offset))
                break
    assert len(sites) == 1, "original before-cursor listener return grammar changed"
    call_offset, unpack_offset = sites[0]
    # 3.13 get_instructions omits CACHE items even with show_caches=True.
    # Both supported interpreters expose the original two-byte code units;
    # admit only actual CACHE words between this CALL and its exact UNPACK.
    offsets = frozenset(range(call_offset, unpack_offset, 2))
    assert all(code.co_code[offset] == dis.opmap["CACHE"] for offset in offsets if offset != call_offset), (
        "original before-cursor CALL has a non-cache successor before UNPACK"
    )
    return offsets, call_offset, unpack_offset


_STRUCTURE_ROW_CALL_OFFSETS, _STRUCTURE_ROW_CALL_OFFSET, _STRUCTURE_ROW_UNPACK_OFFSET = _structure_row_callback_site()


_METADATA_CALL_OFFSETS, _METADATA_CALL_LINE = _direct_call_site(_METADATA_VALIDATOR, "find_credential_material", arguments=2)
_CONTEXT_TEXT_SITE = _direct_call_site(_CONTEXT_TEXT, "find_credential_material", arguments=2)
_CONTEXT_CONTENT_SITE = _direct_call_site(_CONTEXT_CONTENT, "_text", loads_expected=2, arguments=1)
_CONTEXT_MESSAGES_SITE = _direct_call_site(_CONTEXT_MESSAGES, "_message_content", arguments=1)
_CONTEXT_CAPTURE_SITE = _direct_call_site(_CONTEXT_VALIDATOR, "_check_messages", arguments=2)
_CONTEXT_CANONICAL_SITE = _direct_call_site(_CONTEXT_VALIDATOR, "canonical_json", arguments=1)
_SNAPSHOT_SOURCE_SITE = _direct_call_site(_LOAD_SNAPSHOT, "decode_continuation_snapshot", arguments=1, load_op="LOAD_FAST")
_CHUNK_SOURCE_SITE = _direct_call_site(_LOAD_CHUNKS, "verify_private_chunk", arguments=2)
_CHUNK_PARSE_SITE = _direct_call_site(continuation_codec._verify_private_chunk_canonical.__code__, "parse_json_strict", arguments=1)
_SNAPSHOT_PARSE_SITE = _direct_call_site(
    continuation_codec._decode_continuation_snapshot_uncached.__code__, "parse_json_strict", arguments=1
)
_ENCODER_PARSE_SITE = _direct_call_site(_ENCODE_CHUNK, "parse_json_strict", arguments=1)
_READ_CHUNK = stored_continuation.read_chunk
_READ_PRIVATE_CHUNK = stored_continuation.read_private_chunk
_READ_PRIVATE_CHUNK_CODE = _READ_PRIVATE_CHUNK.__code__
_CHUNK_READ_SITE = _direct_call_site(_LOAD_CHUNKS, "read_private_chunk", arguments=4, keywords=("session_id", "operation_id", "reference"))
_NESTED_SIZE_SITE = _direct_call_site(_NESTED_CHUNKS, "utf8_size", arguments=2, keywords=("limit",))
_NESTED_LOADER_SITE = _direct_call_site(_LOAD_CHUNKS, "nested_private_chunks", arguments=1)
_CHUNK_SUCCESS_RETURNS = frozenset(
    item.offset for item in dis.get_instructions(_CHUNK_REQUEST) if item.opname in ("RETURN_VALUE", "RETURN_CONST")
)
_SNAPSHOT_DECODE_BYTES_SITE = _direct_call_site(_LOAD_SNAPSHOT, "decode", arguments=2, load_op="LOAD_ATTR", keywords=("errors",))
_CHUNK_DECODE_BYTES_SITE = _direct_call_site(_LOAD_CHUNKS, "decode", arguments=2, load_op="LOAD_ATTR", keywords=("errors",))
_CHUNK_LENGTH_SITE = _direct_call_site(_CHUNK_REQUEST, "len", arguments=1)
_CHUNK_DIGEST_SITE = _direct_call_site(_CHUNK_REQUEST, "sha256", arguments=1)
_SNAPSHOT_WORK_SITE = _direct_call_site(_SNAPSHOT_VALUE_CODE, "producer", arguments=1, load_op="LOAD_FAST")
_CHUNK_WORK_SITE = _direct_call_site(_CHUNK_VALUE_CODE, "producer", arguments=1, load_op="LOAD_FAST")


def _artifact_query_site(code: CodeType, attribute: str, family: ArtifactFamily) -> tuple[range, int]:
    """Select the outer query CALL, not its nested complete-key CALL."""
    instructions = tuple(dis.get_instructions(code))
    loads = [item for item in instructions if item.opname == "LOAD_ATTR" and item.argval == attribute]
    assert len(loads) == 1, f"{family} artifact original query binding changed"
    load = loads[0]
    calls = [
        (index, item)
        for index, item in enumerate(instructions)
        if item.opname == "CALL"
        and (item.positions.lineno, item.positions.col_offset) == (load.positions.lineno, load.positions.col_offset)
    ]
    assert len(calls) == 1 and calls[0][1].arg == 2, f"{family} artifact original query callsite changed"
    index, call = calls[0]
    assert call.positions.lineno is not None
    return range(call.offset, instructions[index + 1].offset), call.positions.lineno


def _snapshot_query_site() -> tuple[range, int]:
    return _artifact_query_site(_SNAPSHOT_REQUEST, "snapshot", "snapshot")


_SNAPSHOT_QUERY_SITE = _snapshot_query_site()
_CHUNK_QUERY_SITE = _artifact_query_site(_CHUNK_REQUEST, "private_chunk", "chunk")


def _snapshot_source_utf8_extent(encoded: str) -> int:
    """Post-observation source sizing; never production-native byte evidence."""
    return len(encoded.encode("utf-8"))


def _chunk_source_utf8_extent(canonical: str) -> int:
    """Post-observation source extent, independent of each native binding."""
    return len(canonical.encode("utf-8"))


@dataclass(frozen=True, slots=True)
class SnapshotArtifactTotals:
    stage: int
    group: int
    producer: str
    basis: str
    requests: int
    work_attempts: int
    source_utf8_extent: int | None
    processed_source_utf8_extent: int | None


@dataclass(frozen=True, slots=True)
class SnapshotArtifactStageTotals:
    stage: int
    basis: str
    distinct_inputs: int
    requests: int
    work_attempts: int
    observed_work_attempts: int
    numerator: int | None
    denominator: int | None


@dataclass(slots=True)
class _ArtifactRequest:
    record_ordinal: int
    memo_id: int
    thread_id: int
    expected_memo_id: int | None
    key_id: int | None = None


@dataclass(frozen=True, slots=True)
class ChunkArtifactTotals(SnapshotArtifactTotals):
    """Same numeric shape; a separate complete chunk verification owner."""


@dataclass(frozen=True, slots=True)
class ChunkArtifactStageTotals(SnapshotArtifactStageTotals):
    """Independent per-stage chunk arithmetic."""


@dataclass(frozen=True, slots=True)
class _CatalogSchemaBinding:
    plugin: type
    kind: str
    preceding: tuple[type, ...]
    defining: type
    descriptor: classmethod
    function: FunctionType
    code: CodeType
    mro: tuple[type, ...]


@dataclass(frozen=True, slots=True)
class _CatalogSchemaDescriptorGuard:
    defining: type
    descriptor: classmethod
    function: FunctionType
    code: CodeType


@dataclass(frozen=True, slots=True)
class CatalogSchemaTotals:
    stage: int
    factory_attempts: int
    plugin_attempts: int
    read_attempts: int
    attributed_factory_attempts: int
    attributed_plugin_attempts: int
    attributed_read_attempts: int


@dataclass(slots=True)
class MemoInspection:
    family: str
    memo: RetentionStageMemo
    key: object | None = None
    producer: FunctionType | None = None
    attempts: Counter[str] = field(default_factory=Counter)
    actual_policies: list[CredentialPolicyKey | None] = field(default_factory=list)
    byte_calls: Counter[str] = field(default_factory=Counter)
    byte_counts: Counter[str] = field(default_factory=Counter)
    snapshot_artifact_attempts: int = 0
    snapshot_artifact_bound: bool = False
    chunk_artifact_attempts: int = 0
    chunk_artifact_bound: bool = False

    @property
    def group(self):
        return None if self.key is None or self.producer is None else (id(self.memo), id(self.producer), self.key)


@dataclass(frozen=True, slots=True)
class _ProfileHookOrigin:
    hook: FunctionType
    previous: FunctionType | None
    previous_cell: CellType
    trace_cell: CellType
    state_cell: CellType


@dataclass(slots=True)
class _ProfileHookState:
    prior_failed: bool = False
    origin: _ProfileHookOrigin | None = None


@dataclass(frozen=True, slots=True)
class MemoCallSite:
    filename: str
    qualname: str
    first_line: int
    current_line: int


@dataclass(frozen=True, slots=True)
class NativeByteErrorDiagnostic:
    error_index: int
    message: str
    stage: int | None
    memo_id: int | None
    memo_active: bool | None
    thread_id: int
    expected_memo_id: int | None
    owner_kind: Literal["memo_request", "uncached_validation", "context_capture"] | None
    owner_ordinal: int | None
    family: str | None
    ancestry: tuple[MemoCallSite, ...]
    ancestry_truncated: bool


@dataclass(frozen=True, slots=True)
class _PendingChunkParse:
    """Numeric association while the original verifier still owns its text."""

    record_ordinal: int
    memo_id: int
    text_id: int
    thread_id: int
    expected_memo_id: int | None


@dataclass(frozen=True, slots=True)
class UncachedValidationAttempt:
    """One real direct detector call; no input value or retained frame."""

    caller: MemoCallSite
    policy_id: int
    policy: CredentialPolicyKey
    scalar_kind: Literal["str"]
    input_characters: int
    ancestry: tuple[MemoCallSite, ...] = ()


@dataclass(slots=True)
class UncachedValidationInvocation:
    memo: RetentionStageMemo
    invocation: int
    thread_id: int
    validator: UncachedValidator = _METADATA_VALIDATOR_NAME
    attempts: list[UncachedValidationAttempt] = field(default_factory=list)
    exited: bool = False
    expected_memo_id: int | None = None
    byte_calls: Counter[str] = field(default_factory=Counter)
    byte_counts: Counter[str] = field(default_factory=Counter)


@dataclass(slots=True)
class _ScannerSizeInspection:
    """Numeric receipt only; the producer retains its text, chunks and frame."""

    owner: MemoInspection | UncachedValidationInvocation
    caller: int
    text: int
    characters: int
    limit: int
    thread: int
    expected_memo_id: int | None
    call_code: CodeType | None = None
    call_offset: int | None = None
    profile_offset: int | None = None
    private_resource: bool = False
    chunks: int = 0
    encoded_bytes: int = 0
    branch: Literal["length_excess", "ascii", "encoded_excess", "encoded"] | None = None
    returned_size: int | None = None


@dataclass(slots=True)
class _PrivateSizeExpectation:
    """One size obligation created only by an original successful loader verify."""

    owner: MemoInspection
    loader: int
    reference: int
    text: int
    characters: int
    byte_length: int
    thread: int
    expected_memo_id: int | None
    calls: int = 0
    inspection: _ScannerSizeInspection | None = None


@dataclass(frozen=True, slots=True)
class UncachedValidationTotals:
    stage: int
    validator: UncachedValidator
    invocations: int
    attempts: int
    input_characters: int


@dataclass(frozen=True, slots=True)
class UncachedValidationBudget:
    """Finite observation-window charges, separate from product performance."""

    stage: int
    max_invocations: int
    max_attempts: int
    max_input_characters: int
    validator: UncachedValidator = _METADATA_VALIDATOR_NAME


@dataclass(frozen=True, slots=True)
class DetectorStageTotals:
    stage: int
    native_attempts: int
    memo_family_attempts: int
    uncached_validation_attempts: int
    orphan_attempts: int


@dataclass(frozen=True, slots=True)
class MemoOrphanDiagnostic:
    """First unmatched call's code and policy shape, never inspected DATA."""

    memo_id: int
    work: str
    thread_id: int
    submission_expectation: Literal["no-preexisting-carrier", "preexisting-carrier"]
    expected_memo_id: int | None
    inherited_family: str | None
    ancestry: tuple[MemoCallSite, ...]
    ancestry_truncated: bool
    policy_id: int | None
    nominal_policy: bool
    default_policy: bool
    policy_bounds: tuple[int, int, int, int] | None
    policy_collection_sizes: tuple[int | None, int | None] | None


def _orphan_diagnostic(
    frame: FrameType, memo_id: int, work: str, expected_id: int | None, inherited: MemoInspection | None
) -> MemoOrphanDiagnostic:
    sites = []
    ancestor: FrameType | None = frame
    for _ in range(64):
        if ancestor is None:
            break
        code = ancestor.f_code
        sites.append(MemoCallSite(code.co_filename, code.co_qualname, code.co_firstlineno, ancestor.f_lineno))
        ancestor = ancestor.f_back
    policy = frame.f_locals.get("policy") if work == "detector_work" else None
    nominal = type(policy) is credential_material.CredentialTraversalPolicy
    bounds = None
    sizes = None
    if nominal:
        candidate = (policy.max_depth, policy.max_nodes, policy.max_width, policy.max_string_bytes)
        if all(type(value) is int for value in candidate):
            bounds = candidate
        # Do not enumerate reference names or invoke foreign collection methods.
        names, fields = policy.env_ref_names, policy.additional_credential_fields
        names_kind, fields_kind = type(names), type(fields)
        sizes = (
            len(names)
            if names_kind is frozenset or names_kind is set or names_kind is tuple or names_kind is list or names_kind is dict
            else None,
            len(fields)
            if fields_kind is frozenset or fields_kind is set or fields_kind is tuple or fields_kind is list or fields_kind is dict
            else None,
        )
    return MemoOrphanDiagnostic(
        memo_id,
        work,
        get_ident(),
        "no-preexisting-carrier" if expected_id is None else "preexisting-carrier",
        expected_id,
        None if inherited is None else inherited.family,
        tuple(sites),
        ancestor is not None,
        None if policy is None else id(policy),
        nominal,
        policy is credential_material.DEFAULT_CREDENTIAL_TRAVERSAL_POLICY,
        bounds,
        sizes,
    )


def _policy_data(policy: object) -> CredentialPolicyKey | None:
    if type(policy) is not credential_material.CredentialTraversalPolicy:
        return None
    collections = []
    for names in (policy.env_ref_names, policy.additional_credential_fields):
        kind = type(names)
        if not (kind is frozenset or kind is set or kind is tuple or kind is list or kind is dict):
            return None
        if len(names) > 512:
            return None
        items = []
        for index, name in enumerate(names):
            if index >= 512 or type(name) is not str:
                return None
            items.append(name)
        collections.append(frozenset(items))
    bounds = (policy.max_depth, policy.max_nodes, policy.max_width, policy.max_string_bytes)
    if not all(type(value) is int for value in bounds):
        return None
    return CredentialPolicyKey(*collections, *bounds, credential_material.CREDENTIAL_DETECTOR_VERSION)


_METADATA_POLICY_KEY = _policy_data(_METADATA_POLICY)
assert _METADATA_POLICY_KEY is not None, "diagnostic metadata policy lost its closed shape"
_CONTEXT_POLICY_KEY = _policy_data(_CONTEXT_POLICY)
assert _CONTEXT_POLICY_KEY is not None, "provider context policy lost its closed shape"


def _safe_key(key: object) -> bool:
    if type(key) is SnapshotDecodeKey:
        return type(key.encoded) is str
    if type(key) is PrivateChunkVerificationKey:
        reference = key.reference
        if type(reference) is not PrivateChunkRef or type(key.canonical) is not str:
            return False
        try:
            return all(
                actual is expected
                for actual, expected in zip(
                    (type(reference.codec), type(reference.digest), type(reference.byte_length)), (str, str, int), strict=True
                )
            )
        except AttributeError:
            return False
    scan = key.scan if type(key) is StateCredentialStepKey else key
    if type(scan) is not CredentialScanKey or type(scan.content) is not tuple:
        return False
    if not all(type(value) is str for value in (scan.scanner_role, scan.surface)) or type(scan.detector) is not FunctionType:
        return False
    if any(
        type(token) is not CredentialInputToken or type(token.kind) is not str or type(token.value) is not str for token in scan.content
    ):
        return False
    policy = scan.policy
    return (
        type(policy) is CredentialPolicyKey
        and all(
            type(names) is frozenset and all(type(name) is str for name in names)
            for names in (policy.env_ref_names, policy.additional_credential_fields)
        )
        and all(type(value) is int for value in (policy.max_depth, policy.max_nodes, policy.max_width, policy.max_string_bytes))
        and type(policy.detector_version) is str
    )


class RetentionMemoTrace:
    """One observation window per loop thread; no frames or SQL owners retained."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        request: pytest.FixtureRequest,
        *,
        require_native_bytes: bool = False,
        require_snapshot_artifacts: bool = False,
        require_chunk_artifacts: bool = False,
        require_catalog_schema: bool = False,
        catalog: catalog_service.CatalogServiceImpl | None = None,
    ) -> None:
        assert type(require_native_bytes) is bool, "native byte observation requires explicit boolean opt-in"
        assert type(require_snapshot_artifacts) is bool, "snapshot artifact observation requires explicit boolean opt-in"
        assert type(require_chunk_artifacts) is bool, "chunk artifact observation requires explicit boolean opt-in"
        assert type(require_catalog_schema) is bool, "catalog schema observation requires explicit boolean opt-in"
        assert (type(catalog) is _CATALOG_CLASS) if require_catalog_schema else catalog is None, (
            "catalog schema observation requires its exact catalog owner"
        )
        self.memos: list[RetentionStageMemo] = []
        self.records: list[MemoInspection] = []
        self.uncached_validations: list[UncachedValidationInvocation] = []
        self.detector_attempts: Counter[RetentionStageMemo] = Counter()
        self.populated: dict[RetentionStageMemo, int] = {}
        self.published: list[tuple[int, int, object]] = []
        self.orphans: list[tuple[int, str, str, int]] = []
        self.first_orphan: MemoOrphanDiagnostic | None = None
        self.excluded: Counter[str] = Counter()
        self.errors: list[str] = []
        self._frames: dict[int, MemoInspection] = {}
        self._validator_frames: dict[int, UncachedValidationInvocation] = {}
        self._verified_uncached_totals: tuple[UncachedValidationTotals, ...] | None = None
        self._terminal_uncached_totals: tuple[UncachedValidationTotals, ...] = ()
        self._lock = Lock()
        self._owner_thread = get_ident()
        self._closed = False
        self._structure_lifetime_monitor: _StructureLifetimeMonitor | None = None
        self._structure_required = False
        self._structure_closed = False
        self._structure_closing = False
        self._structure_entrypoint = "calibration"
        self._structure_errors: list[str] = []
        self._structure_error_count = 0
        self._structure_observer_incomplete = False
        self._structure_observation_verified = False
        self._structure_engines: tuple[Engine, ...] = ()
        self._structure_listeners: list[tuple[Engine, str, Callable[..., None]]] = []
        self._structure_threads: dict[int, _StructureThread] = {self._owner_thread: _StructureThread(self._owner_thread)}
        self._structure_row_codes: dict[_StructureRowCodeIdentity, _StructureRowCodeFacts] = {}
        adapter = _STRUCTURE_ROW_ADAPTER_CODE_REF()
        if adapter is not None:
            self._structure_row_code_facts(adapter)
        self._structure_memo_clocks: dict[int, _StructureInvocation] = {}
        self._structure_submissions: dict[int, tuple[int, str, int | None, int | None, int]] = {}
        self._structure_connections: dict[int, tuple[int, int]] = {}
        self._structure_scope_wrapper = None
        self._structure_submit_wrapper = None
        self._terminal_structure_state: tuple[object, ...] = ()
        self._finished = False
        self._terminal_errors: tuple[str, ...] = ()
        self._observation_incomplete = False
        self._terminal_incomplete = False
        self._installed = False
        self._report_name = "retention-memo-observation-" + str(id(self))
        self._report_item: pytest.Item | None = None
        self._report_manager: pytest.PytestPluginManager | None = None
        self._report_registered = False
        self.require_native_bytes = require_native_bytes
        self.byte_errors: list[str] = []
        self.byte_error_diagnostics: list[NativeByteErrorDiagnostic] = []
        self._byte_diagnostic_sites: set[tuple[object, ...]] = set()
        self._byte_diagnostic_lock = Lock()
        self._byte_pending_parses: dict[int, list[_PendingChunkParse]] = {}
        self._byte_detector_frames: dict[int, MemoInspection | UncachedValidationInvocation] = {}
        self._byte_size_pending: dict[int, _ScannerSizeInspection] = {}
        self._byte_size_frames: dict[int, _ScannerSizeInspection] = {}
        self._byte_size_inspections: list[_ScannerSizeInspection] = []
        self._byte_canonical_buffers: dict[int, tuple[int, int]] = {}
        self._byte_canonical_owners: dict[int, MemoInspection | ContextCanonicalInspection] = {}
        self.context_canonical_inspections: list[ContextCanonicalInspection] = []
        self._context_byte_frames: dict[int, ContextCanonicalInspection] = {}
        self._byte_sources: dict[int, tuple[int, int]] = {}
        self._byte_private_payloads: dict[int, tuple[int, int]] = {}
        self._byte_private_expected: dict[int, _PrivateSizeExpectation] = {}
        self._byte_private_requests: Counter[int] = Counter()
        self._byte_private_sizes: list[_PrivateSizeExpectation] = []
        self._byte_threads: dict[int, int | None] = {get_ident(): None}
        self._byte_monitor: _ByteMonitor | None = None
        self._verified_byte_totals: tuple[NativeByteTotals, ...] | None = None
        self._terminal_byte_totals: tuple[NativeByteTotals, ...] = ()
        self._terminal_byte_errors: tuple[str, ...] = ()
        self._terminal_byte_diagnostics: tuple[NativeByteErrorDiagnostic, ...] = ()
        self._terminal_private_sizes: tuple[tuple, ...] = ()
        self.require_snapshot_artifacts = require_snapshot_artifacts
        self.snapshot_artifact_errors: list[str] = []
        self._snapshot_artifact_requests: dict[int, _ArtifactRequest] = {}
        self._snapshot_artifact_observed: Counter[RetentionStageMemo] = Counter()
        self._snapshot_artifact_sized = False
        self._snapshot_artifact_sizing_failed = False
        self._snapshot_artifact_verified = False
        self._snapshot_artifact_rows: tuple[SnapshotArtifactTotals, ...] = ()
        self._snapshot_artifact_stages: tuple[SnapshotArtifactStageTotals, ...] = ()
        self._terminal_snapshot_artifact_errors: tuple[str, ...] = ()
        self._terminal_snapshot_artifact_rows: tuple[SnapshotArtifactTotals, ...] = ()
        self._terminal_snapshot_artifact_stages: tuple[SnapshotArtifactStageTotals, ...] = ()
        self._terminal_snapshot_artifact_verified = False
        self.require_chunk_artifacts = require_chunk_artifacts
        self.chunk_artifact_errors: list[str] = []
        self._chunk_artifact_requests: dict[int, _ArtifactRequest] = {}
        self._chunk_artifact_observed: Counter[RetentionStageMemo] = Counter()
        self._chunk_artifact_sized = False
        self._chunk_artifact_sizing_failed = False
        self._chunk_artifact_verified = False
        self._chunk_artifact_rows: tuple[ChunkArtifactTotals, ...] = ()
        self._chunk_artifact_stages: tuple[ChunkArtifactStageTotals, ...] = ()
        self._terminal_chunk_artifact_errors: tuple[str, ...] = ()
        self._terminal_chunk_artifact_rows: tuple[ChunkArtifactTotals, ...] = ()
        self._terminal_chunk_artifact_stages: tuple[ChunkArtifactStageTotals, ...] = ()
        self._terminal_chunk_artifact_verified = False
        self.require_catalog_schema = require_catalog_schema
        self._catalog_schema_owner = catalog
        self._catalog_schema_manager = None
        self._catalog_schema_groups: tuple[tuple[str, str, str, tuple[type, ...]], ...] = ()
        self._catalog_schema_bindings: tuple[_CatalogSchemaBinding, ...] = ()
        self._catalog_schema_slot_guards: tuple[type, ...] = ()
        self._catalog_schema_descriptor_guards: tuple[_CatalogSchemaDescriptorGuard, ...] = ()
        self._catalog_schema_codes: dict[int, CodeType] = {
            id(code): code for _owner, _descriptor, _function, code in _CATALOG_SCHEMA_ORIGINALS
        }
        self._catalog_schema_scopes: dict[int, list[int]] = {}
        self._catalog_schema_frames: dict[int, tuple[int, str]] = {}
        self._catalog_schema_raw: dict[RetentionStageMemo, Counter[str]] = {}
        self._catalog_schema_attributed: dict[RetentionStageMemo, Counter[str]] = {}
        self._catalog_schema_unassigned: Counter[str] = Counter()
        self._catalog_schema_checked: set[RetentionStageMemo] = set()
        self._catalog_schema_lock = Lock()
        self._catalog_schema_errors: list[str] = []
        self._catalog_schema_error_count = 0
        self._catalog_schema_source_valid = False
        self._catalog_schema_verified = False
        self._terminal_catalog_schema_state: tuple[object, ...] = ()
        if require_catalog_schema:
            self._bind_catalog_schema()
        self._query_codes = {id(code): code for code in _QUERY_CODES}
        for function in (
            RetentionStageMemo.snapshot,
            RetentionStageMemo.private_chunk,
            RetentionStageMemo.credential,
            RetentionStageMemo.state_credentials,
        ):
            if type(function) is not FunctionType:
                raise RuntimeError("memo observer cannot inspect this query binding")
            self._query_codes[id(function.__code__)] = function.__code__
        self._previous = sys.getprofile()
        if self._previous is not None and type(self._previous) is not FunctionType:
            raise RuntimeError("memo observer refuses unsupported prior profiler without replacing it")
        self._main_hook, self._main_state = self._make_hook(self._previous)
        if self._previous is not None and self._previous.__code__ is self._main_hook.__code__:
            raise RuntimeError("memo observation windows cannot overlap on one loop thread")
        self._owned_hooks = {self._main_hook}
        original_scope = continuation_clock.retention_stage_memo_scope
        original_submit = async_workers._submit_shared

        @contextmanager
        def scope() -> Iterator[RetentionStageMemo]:
            with original_scope() as memo:
                with self._lock:
                    if self._closed:
                        self.errors.append("retention scope opened after observation finalized")
                    self.memos.append(memo)
                    if self.require_catalog_schema:
                        self._catalog_schema_scopes.setdefault(get_ident(), []).append(id(memo))
                if self._structure_required:
                    self._structure_profile(sys._getframe(), "scope_enter", id(memo), None)
                try:
                    yield memo
                finally:
                    if self.require_catalog_schema:
                        stack = self._catalog_schema_scopes.get(get_ident())
                        if not stack or stack[-1] != id(memo):
                            self._catalog_schema_error("catalog schema scope custody changed")
                        else:
                            stack.pop()
                        if stack is not None and not stack:
                            self._catalog_schema_scopes.pop(get_ident(), None)
                    try:
                        # Existing keys only. Never invoke work to fill evidence.
                        with memo._lock:
                            entries = [*memo._snapshots, *memo._chunks, *memo._credentials, *memo._states]
                        with self._lock:
                            self.populated[memo] = len(entries)
                            for producer, key in entries:
                                if type(producer) is FunctionType and _safe_key(key):
                                    self.published.append((id(memo), id(producer), key))
                                else:
                                    self.errors.append("unsupported published memo key")
                    except Exception as error:
                        with self._lock:
                            self.errors.append("scope observation: " + type(error).__name__)

        async def submit(callable_, ticket=None, finalizer=None, *, deferred_cancellations=None, watch_reader=None):
            actual = current_retention_stage_memo()
            expected_id = None if actual is None else id(actual)
            if type(ticket) is not RequiredWorkTicket or finalizer is not None or watch_reader is not None:
                if actual is not None:
                    with self._lock:
                        self.excluded["prebound-or-no-ordinary-ticket"] += 1
                # Preserve supplied MethodType/identity checks byte-for-byte.
                return await original_submit(
                    callable_, ticket, finalizer, deferred_cancellations=deferred_cancellations, watch_reader=watch_reader
                )

            def observed():
                previous = sys.getprofile()
                if previous is not None and type(previous) is not FunctionType:
                    with self._lock:
                        self.errors.append("unsupported worker prior profiler; native callable delegated unchanged")
                    return callable_()
                if previous in self._owned_hooks:
                    if self._structure_required:
                        _structure_refuse(self, "worker_profile_reentry")
                    return callable_()
                hook, state = self._make_hook(previous, expected_id=expected_id)
                with self._lock:
                    self._owned_hooks.add(hook)
                    self._byte_threads[get_ident()] = expected_id
                if self._structure_required and get_ident() not in self._structure_threads:
                    self._structure_threads[get_ident()] = _StructureThread(get_ident())
                try:
                    sys.setprofile(hook)
                    if self._structure_required:
                        try:
                            self._structure_profile(sys._getframe(), "worker_install", None, expected_id)
                        except BaseException:
                            self._structure_observer_incomplete = True
                            with suppress(BaseException):
                                _structure_refuse(self, "worker_install_interrupted")
                            raise
                    return callable_()
                finally:
                    primary = sys.exception()
                    try:
                        if self._structure_required:
                            try:
                                self._structure_profile(sys._getframe(), "worker_exit", primary, expected_id)
                            except BaseException:
                                self._structure_observer_incomplete = True
                                with suppress(BaseException):
                                    _structure_refuse(self, "worker_exit_interrupted")
                                if primary is None:
                                    raise
                    finally:
                        pending = sys.exception()
                        try:
                            if not state.prior_failed:
                                if sys.getprofile() is hook:
                                    sys.setprofile(previous)
                                else:
                                    with self._lock:
                                        self.errors.append("worker profiler changed during observation")
                        except BaseException:
                            if self._structure_required:
                                self._structure_observer_incomplete = True
                                with suppress(BaseException):
                                    _structure_refuse(self, "worker_profile_restoration_interrupted")
                            if pending is None or not self._structure_required:
                                raise
                        finally:
                            with self._lock:
                                self._owned_hooks.discard(hook)
                                self._byte_threads.pop(get_ident(), None)

            return await original_submit(
                observed, ticket, finalizer, deferred_cancellations=deferred_cancellations, watch_reader=watch_reader
            )

        self._structure_scope_wrapper = scope
        self._structure_submit_wrapper = submit
        if require_native_bytes:
            self._byte_monitor = _ByteMonitor(self._byte_call)
        try:
            request.addfinalizer(self._finalize)
            self._report_item = request.node
            self._report_manager = request.config.pluginmanager
            self._report_manager.register(self, self._report_name)
            if self._report_manager.get_plugin(self._report_name) is not self:
                raise RuntimeError("memo observation report cleanup was not registered")
            self._report_registered = True
            monkeypatch.setattr(continuation_clock, "retention_stage_memo_scope", scope)
            monkeypatch.setattr(async_workers, "_submit_shared", submit)
            sys.setprofile(self._main_hook)
            self._installed = True
        except BaseException as error:
            self.close(primary=error)
            raise

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_makereport(self, item, call):
        # Fixture finalizers run after the call report is rendered. Close only
        # this window's failed call first; its unfinished finalizer stays strict.
        if item is self._report_item and call.when == "call" and call.excinfo is not None:
            self.close(primary=call.excinfo.value)
        return (yield)

    def _make_hook(self, previous, *, expected_id: int | None = None):
        state = _ProfileHookState()

        def hook(frame, event, arg):
            if previous is not None:
                try:
                    previous(frame, event, arg)
                except BaseException:
                    state.prior_failed = True
                    # Monotonic receipt invalidation must not acquire our
                    # lock: the prior hook may fail while this thread holds it.
                    self._observation_incomplete = True
                    raise
            try:
                self._event(frame, event, arg, expected_id)
                if self.require_native_bytes:
                    self._byte_profile(frame, event, arg, expected_id)
                if self.require_snapshot_artifacts:
                    self._snapshot_artifact_event(frame, event, expected_id)
                if self.require_chunk_artifacts:
                    self._chunk_artifact_event(frame, event, expected_id)
                if self.require_catalog_schema:
                    self._catalog_schema_event(frame, event, expected_id)
                if self._structure_required:
                    self._structure_profile(frame, event, arg, expected_id)
            except Exception as error:
                # Invalidate accounting separately, preserving native outcomes.
                with self._lock:
                    self.errors.append("observer callback: " + type(error).__name__)

        cells = dict(zip(hook.__code__.co_freevars, hook.__closure__, strict=True))
        state.origin = _ProfileHookOrigin(hook, previous, cells["previous"], cells["self"], cells["state"])
        return hook, state

    def observe_structure(self, *, engines: tuple[Engine, ...], entrypoint: Literal["calibration", "first_compose"]) -> None:
        assert not self._closed and not self._structure_required, "structure observation requires one open explicit mode"
        assert type(engines) is tuple and engines and all(type(engine) is Engine for engine in engines)
        assert len({id(engine) for engine in engines}) == len(engines), "structure engine owners must be distinct"
        assert type(entrypoint) is str and entrypoint in ("calibration", "first_compose")
        assert len(engines) == 1 if entrypoint == "first_compose" else len(engines) <= 2
        self._structure_required = True
        self._structure_entrypoint = entrypoint
        self._structure_engines = engines
        try:
            current = self._structure_sources_current()
        except Exception:
            if self._observation_incomplete:
                raise
            current = False
        if not current:
            _structure_refuse(self, "original_source_changed")
        self._structure_lifetime_monitor = _StructureLifetimeMonitor(self)
        try:
            self._structure_lifetime_monitor.install()
        except BaseException as primary:
            _structure_refuse(self, "lifetime_monitor_install_failed")
            self._structure_close(primary)
            raise
        try:
            for engine in engines:
                callbacks = [
                    ("before_cursor_execute", self._structure_before_cursor_execute),
                    ("after_cursor_execute", self._structure_after_cursor_execute),
                ]
                for kind in ("begin", "commit", "rollback"):

                    def transaction(conn, kind=kind):
                        self._structure_transaction_attempt(conn, kind)

                    callbacks.append((kind, transaction))
                for name, callback in callbacks:
                    # Ownership precedes a possibly successful-then-raising install.
                    self._structure_listeners.append((engine, name, callback))
                    sql_events.listen(engine, name, callback)
                    if not sql_events.contains(engine, name, callback):
                        _structure_refuse(self, "listener_not_installed")
        except BaseException as primary:
            _structure_refuse(self, "listener_install_failed")
            self._structure_close(primary)
            raise

    def structure_listener_handles(self) -> tuple[tuple[Engine, str, Callable[..., None]], ...]:
        return tuple(self._structure_listeners)

    def _structure_exchange_supported(self) -> bool:
        # None has no callback delegate. Otherwise prove the ACTUAL owned
        # function/cells and its frozen birth origin, not self._previous/name.
        hook = _LIFETIME_GET_PROFILE()
        if hook is None:
            return True
        if type(hook) is not FunctionType or not any(hook is known for known in self._owned_hooks):
            return False
        if (
            hook.__code__ is not _LIFETIME_PROFILE_CODE
            or hook.__globals__ is not _LIFETIME_PROFILE_FACTORY.__globals__
            or RetentionMemoTrace._make_hook is not _LIFETIME_PROFILE_FACTORY
            or _LIFETIME_PROFILE_FACTORY.__code__ is not _LIFETIME_PROFILE_FACTORY_CODE
            or hook.__closure__ is None
        ):
            return False
        closure = hook.__closure__
        if len(closure) != len(_LIFETIME_PROFILE_FREEVARS):
            return False
        previous_cell = closure[_LIFETIME_PROFILE_CELL_INDEXES[0]]
        trace_cell = closure[_LIFETIME_PROFILE_CELL_INDEXES[1]]
        state_cell = closure[_LIFETIME_PROFILE_CELL_INDEXES[2]]
        state = state_cell.cell_contents
        if type(state) is not _ProfileHookState:
            return False
        origin = state.origin
        return (
            type(origin) is _ProfileHookOrigin
            and origin.hook is hook
            and origin.previous is None
            and origin.previous_cell is previous_cell
            and previous_cell.cell_contents is None
            and origin.trace_cell is trace_cell
            and trace_cell.cell_contents is self
            and origin.state_cell is state_cell
            and state_cell.cell_contents is state
        )

    def _structure_sources_current(self) -> bool:
        if not _structure_sql_sources_current():
            return False
        monitor = self._structure_lifetime_monitor
        if monitor is not None and not monitor.installing and not monitor.current():
            return False
        for function, code in _STRUCTURE_FUNCTION_CODES:
            if not (type(function) is FunctionType and function.__code__ is code):
                return False
        if (
            continuation_clock.__dict__.get("ContinuationActionClock") is not _STRUCTURE_CLOCK_CLASS
            or _STRUCTURE_CLOCK_CLASS.__dict__.get("run") is not _STRUCTURE_CLOCK
            or Engine.__dict__.get("begin") is not _STRUCTURE_SQLA_ENGINE_BEGIN
        ):
            return False
        for engine in self._structure_engines:
            dialect = engine.dialect
            if type(dialect) not in (SQLiteDialect_pysqlite, PGDialect_psycopg):
                return False
            if dialect.statement_compiler is not (SQLiteCompiler if type(dialect) is SQLiteDialect_pysqlite else PGCompiler_psycopg):
                return False
            method = engine.begin
            if type(method) is not MethodType or method.__self__ is not engine:
                return False
            if engine.dialect.name == "sqlite":
                factory = method.__func__
                wrapped = factory.__dict__.get("__wrapped__")
                if (
                    factory.__code__ is not _STRUCTURE_CONTEXT_FACTORY_CODE
                    or type(wrapped) is not FunctionType
                    or wrapped.__code__ is not _STRUCTURE_ENGINE_BEGIN
                    or wrapped.__globals__ is not _STRUCTURE_ENGINE_FACTORY.__globals__
                    or factory.__closure__ is None
                    or len(factory.__closure__) != 1
                    or factory.__closure__[0].cell_contents is not wrapped
                ):
                    return False
            elif method.__func__ is not _STRUCTURE_SQLA_ENGINE_BEGIN:
                return False
        for function, code in _STRUCTURE_FUNCTION_CODES:
            if not (type(function) is FunctionType and function.__code__ is code):
                return False
        if not (
            continuation_clock.ContinuationActionClock.run is _STRUCTURE_CLOCK
            and continuation_clock.ContinuationActionClock is _STRUCTURE_CLOCK_CLASS
            and Engine.begin is _STRUCTURE_SQLA_ENGINE_BEGIN
            and SQLiteCompiler.construct_params is _STRUCTURE_CONSTRUCT_PARAMS
            and PGCompiler_psycopg.construct_params is _STRUCTURE_CONSTRUCT_PARAMS
            and Connection._exec_single_context is _STRUCTURE_SINGLE_CONTEXT
        ):
            return False
        for dialect in (SQLiteDialect_pysqlite, PGDialect_psycopg):
            if not (
                dialect.do_execute is _STRUCTURE_CURSOR_EXECUTE
                and dialect.do_execute_no_params is _STRUCTURE_CURSOR_NO_PARAMS
                and dialect.do_executemany is _STRUCTURE_CURSOR_MANY
            ):
                return False
        if not (
            composition_initialization.initialize_continuation_composition is _STRUCTURE_ROOT
            and continuation_first_runtime.restore_first_continuation_runtime is _STRUCTURE_HANDOFF_ROOT
            and session_locking.locked_session_transaction is _STRUCTURE_WINDOW_FACTORY
        ):
            return False
        for name, function in _STRUCTURE_LOCK_BINDINGS:
            if session_locking.__dict__.get(name) is not function:
                return False
        if not (
            _GeneratorContextManager.__enter__.__code__ is _STRUCTURE_CONTEXT_ENTER_CODE
            and _STRUCTURE_WINDOW_FACTORY.__wrapped__ is _STRUCTURE_WINDOW
            and database_clock.database_now is _STRUCTURE_DB_CLOCK
            and stored_continuation.database_now is _STRUCTURE_DB_CLOCK
            and stored_continuation.require_continuation_mutation is _STRUCTURE_ADMISSION
            and ThreadPoolExecutor.submit is _STRUCTURE_EXECUTOR_SUBMIT
            and RequiredInvocationWitness.invoke is _STRUCTURE_WITNESS
            and async_workers.run_required_sql_finish_once is _STRUCTURE_FINISH
            and async_workers._submit_shared is self._structure_submit_wrapper
            and continuation_clock.retention_stage_memo_scope is self._structure_scope_wrapper
            and retention_memo.prepare_retention_sql_callable is _STRUCTURE_PREPARE
            and async_workers.prepare_retention_sql_callable is _STRUCTURE_PREPARE
            and retention_memo._RETENTION_STAGE_MEMO is _CATALOG_STAGE_CONTEXT
            and Connection.execute is _STRUCTURE_CONNECTION_EXECUTE
            and Connection.exec_driver_sql is _STRUCTURE_DRIVER_EXECUTE
            and continuation_first_runtime.locked_session_transaction is _STRUCTURE_WINDOW_FACTORY
            and composition_initialization.locked_session_transaction is _STRUCTURE_WINDOW_FACTORY
            and session_engine.create_session_engine is _STRUCTURE_ENGINE_FACTORY
            and session_engine._SESSION_WRITE_INTENT_OPTION is _STRUCTURE_WRITE_OPTION
        ):
            return False
        for context in (SQLiteExecutionContext, PGExecutionContext_psycopg):
            if not (
                context._init_compiled.__func__ is _STRUCTURE_EXECUTION_INIT
                and context._init_statement.__func__ is _STRUCTURE_STATEMENT_INIT
            ):
                return False
        return True

    def _structure_profile(self, frame: FrameType, event: str, arg: object, expected_id: int | None) -> None:
        if self._structure_closed or self._structure_closing:
            return
        try:
            if event in ("call", "c_call") and not self._structure_sources_current():
                _structure_refuse(self, "original_source_changed")
            identity = get_ident()
            thread = self._structure_threads.get(identity)
            if event == "worker_install":
                caller = frame.f_back
                if caller is None or caller.f_code is not _STRUCTURE_WITNESS.__code__:
                    _structure_refuse(self, "worker_without_original_witness")
                    return
                witness = caller.f_locals.get("self")
                issued = self._structure_submissions.get(id(witness))
                if type(witness) is not RequiredInvocationWitness or issued is None:
                    _structure_refuse(self, "worker_without_actual_submission")
                    return
                if thread is None:
                    thread = _StructureThread(identity)
                    self._structure_threads[identity] = thread
                if thread.submission is not None:
                    _structure_refuse(self, "concurrent_worker_identity")
                thread.submission, thread.worker_phase, thread.worker_clock, thread.expected, _finish = issued
                thread.witness = id(witness)
                thread.worker_enter = thread.worker_terminal = None
                if expected_id != thread.expected:
                    _structure_refuse(self, "worker_submission_expectation_changed")
                if expected_id is None:
                    entered = _structure_emit(self, thread, "worker_enter", source="original_witness_physical")
                    thread.worker_enter = entered.ordinal
                    thread.edges.append(StructureEdge(thread.submission, entered.ordinal, "submit_to_worker"))
                return
            if thread is None:
                _structure_refuse(self, "unowned_physical_thread")
                return
            if event == "worker_exit":
                if thread.submission is None or thread.worker_enter is None:
                    _structure_refuse(self, "worker_terminal_without_entry")
                terminal = _structure_emit(
                    self, thread, "worker_terminal", source="original_witness_exit", outcome="returned" if arg is None else "unwound"
                )
                thread.worker_terminal = terminal.ordinal
                if thread.witness is not None:
                    self._structure_submissions.pop(thread.witness, None)
                thread.witness = None
                thread.submission = None
                thread.worker_phase, thread.worker_clock, thread.expected = "outside_root", None, None
                return
            if event == "scope_enter":
                if not thread.clocks or thread.clocks[-1] not in thread.frames:
                    _structure_refuse(self, "scope_without_original_clock")
                    return
                caller = frame.f_back
                if (
                    caller is None
                    or caller.f_code is not _STRUCTURE_CONTEXT_ENTER_CODE
                    or caller.f_back is None
                    or caller.f_back.f_code is not _STRUCTURE_CLOCK.__code__
                    or id(caller.f_back) != thread.clocks[-1]
                ):
                    _structure_refuse(self, "scope_without_exact_original_clock_callsite")
                    return
                owner = thread.frames[thread.clocks[-1]]
                memo = _CATALOG_STAGE_CONTEXT.get()
                stage = next((index for index, known in enumerate(self.memos, 1) if memo is known), None)
                if (
                    type(memo) is not RetentionStageMemo
                    or id(memo) != arg
                    or owner.phase != "retention"
                    or stage is None
                    or owner.memo is not None
                ):
                    _structure_refuse(self, "scope_carrier_changed")
                    return
                owner.memo, owner.stage = id(memo), stage
                self._structure_memo_clocks[id(memo)] = owner
                return
            if event == "c_return" and frame.f_code is _STRUCTURE_ADAPTER_RUN:
                if type(arg) is BuiltinFunctionType and arg.__self__ is _CATALOG_STAGE_CONTEXT and arg.__name__ == "set":
                    memo = _CATALOG_STAGE_CONTEXT.get()
                    if thread.submission is None or thread.worker_enter is not None or memo is None or id(memo) != thread.expected:
                        _structure_refuse(self, "context_entry_carrier_changed")
                    else:
                        entered = _structure_emit(self, thread, "worker_enter", source="original_fresh_context")
                        thread.worker_enter = entered.ordinal
                        thread.edges.append(StructureEdge(thread.submission, entered.ordinal, "submit_to_worker"))
                return
            self._structure_row_profile(frame, event, arg, thread)
            binding = _STRUCTURE_CODES.get(id(frame.f_code))
            role = binding[1] if binding is not None and binding[0] is frame.f_code else None
            if role is None or event not in ("call", "return"):
                return
            if role == "connection_setup":
                if event == "call":
                    _structure_emit(self, thread, "unsupported_connection_setup_attempt", source="original_sqlite_configure")
                    if frame.f_globals is not _STRUCTURE_ENGINE_FACTORY.__globals__:
                        _structure_refuse(self, "connection_setup_globals_changed")
                    _structure_refuse(self, "raw_DBAPI_connection_setup_unsupported")
                return
            if role in ("context_compiled", "context_statement", "cursor_execute", "cursor_no_params", "cursor_many"):
                code = frame.f_code
                source = next(function for function, label in _STRUCTURE_ROLES if label == role)
                if frame.f_globals is not source.__globals__:
                    _structure_refuse(self, "driver_or_context_globals_changed")
                current = thread.frames.get(id(frame))
                if role in ("cursor_execute", "cursor_no_params", "cursor_many") and event == "return":
                    if current is None:
                        _structure_refuse(self, "driver_terminal_without_entry")
                        return
                    instruction = _STRUCTURE_SITES[code].get(frame.f_lasti)
                    outcome = (
                        "unknown" if instruction is None else "returned" if instruction in ("RETURN_VALUE", "RETURN_CONST") else "unwound"
                    )
                    if outcome == "unknown":
                        _structure_refuse(self, "unknown_driver_terminal_site")
                    request = thread.frames.get(current.request)
                    if request is None or request.role not in ("sql", "driver"):
                        _structure_refuse(self, "driver_request_retired_before_terminal")
                    else:
                        request.driver_outcome = outcome
                    thread.frames.pop(id(frame))
                    return
                if role in ("context_compiled", "context_statement") and event != "return":
                    return
                parent = frame.f_back
                request = None
                request_key = None
                for _ in range(64):
                    if parent is None:
                        break
                    candidate = thread.frames.get(id(parent))
                    if candidate is not None and candidate.role in ("sql", "driver"):
                        request, request_key = candidate, id(parent)
                        break
                    parent = parent.f_back
                if request is None:
                    if role in ("cursor_execute", "cursor_no_params", "cursor_many"):
                        _structure_emit(self, thread, "sql_attempt", source="unattributed_" + role)
                    _structure_refuse(self, "driver_or_context_without_live_request")
                    return
                if role in ("context_compiled", "context_statement"):
                    instruction = _STRUCTURE_SITES[code].get(frame.f_lasti)
                    if instruction not in ("RETURN_VALUE", "RETURN_CONST") or type(arg) not in (
                        SQLiteExecutionContext,
                        PGExecutionContext_psycopg,
                    ):
                        _structure_refuse(self, "context_constructor_did_not_return_owned_context")
                        return
                    if request.execution_context is not None or id(arg.root_connection) != request.connection:
                        _structure_refuse(self, "context_constructor_request_changed")
                        return
                    if role == "context_compiled" and id(arg.invoked_statement) != request.statement:
                        _structure_refuse(self, "context_constructor_statement_changed")
                        return
                    request.execution_context = id(arg)
                    request.sql_signature = _structure_sql_signature(arg.statement)
                    try:
                        request.pending_shape = _structure_sql_shape(self, request, arg)
                    except _StructureShapeRefusal as refusal:
                        if (
                            type(refusal) is _StructureShapeRefusal
                            and type(refusal.guard_code) is str
                            and refusal.guard_code in _STRUCTURE_SHAPE_GUARD_CODES
                        ):
                            _structure_refuse(self, "publication_guard_" + refusal.guard_code)
                        _structure_refuse(self, "unsupported_or_unbound_SQL_shape")
                    except Exception:
                        _structure_refuse(self, "unsupported_or_unbound_SQL_shape")
                    if request.store_role is not None:
                        processed = frame.f_locals.get("core_positional_parameters" if arg.compiled.positional else "core_dict_parameters")
                        if (
                            role != "context_compiled"
                            or arg is not frame.f_locals.get("self")
                            or type(processed) is not list
                            or len(processed) != 1
                            or arg.parameters is not processed
                            or type(processed[0]) not in (tuple, list, dict)
                            or arg.executemany is not False
                        ):
                            _structure_refuse(self, "publication_processed_row_origin_unbound")
                        else:
                            request.row_origin = _StructureRowOrigin(
                                id(arg), id(processed), id(processed[0]), id(arg.cursor), id(frame), id(code), id(frame.f_globals)
                            )
                    return
                # The original dialect receives the final statement and rows
                # after ALL retval listeners. Pair to the context born above,
                # never to a pre-listener guess or a cached template's values.
                context = frame.f_locals.get("context")
                request.driver_attempts += 1
                request.driver_source = role
                driver = _StructureInvocation(
                    role,
                    request.event,
                    request.index,
                    request.phase,
                    request.clock,
                    request.stage,
                    request.memo,
                    request.parent,
                    engine=request.engine,
                    connection=request.connection,
                    request=request_key,
                )
                thread.frames[id(frame)] = driver
                if (
                    type(context) not in (SQLiteExecutionContext, PGExecutionContext_psycopg)
                    or id(context) != request.execution_context
                    or id(context.root_connection) != request.connection
                    or frame.f_locals.get("cursor") is not context.cursor
                    or frame.f_locals.get("self") is not context.dialect
                    or not any(context.dialect is engine.dialect for engine in self._structure_engines)
                    or frame.f_back is None
                    or frame.f_back.f_code is not _STRUCTURE_SINGLE_CONTEXT.__code__
                ):
                    _structure_refuse(self, "final_driver_context_or_callsite_changed")
                    return
                statement = frame.f_locals.get("statement")
                signature = _structure_sql_signature(statement)
                if signature != request.sql_signature or (
                    statement is not context.statement and _structure_sql_signature(context.statement) != request.sql_signature
                ):
                    _structure_refuse(self, "final_driver_statement_changed")
                    return
                shape = request.pending_shape
                if shape is None or shape.executemany is not (role == "cursor_many"):
                    _structure_refuse(self, "final_driver_shape_missing_or_mode_changed")
                    return
                rows = () if role == "cursor_no_params" else frame.f_locals.get("parameters")
                shape = replace(shape, bind_arities=_structure_parameter_arities(rows, role == "cursor_many"))
                thread.events[request.index] = replace(thread.events[request.index], sql=shape)
                if request.store_role is not None:
                    self._structure_row_consume(thread, request, frame.f_back)
                    if not self._structure_row_live(request, frame.f_back) or rows is not frame.f_back.f_locals.get("effective_parameters"):
                        _structure_refuse(self, "publication_final_row_origin_changed")
                    try:
                        _structure_dml_shape(self, request, context, final_parameters=rows)
                    except _StructureShapeRefusal as refusal:
                        if (
                            type(refusal) is _StructureShapeRefusal
                            and type(refusal.guard_code) is str
                            and refusal.guard_code in _STRUCTURE_SHAPE_GUARD_CODES
                        ):
                            _structure_refuse(self, "publication_guard_" + refusal.guard_code)
                        _structure_refuse(self, "publication_final_source_or_layout_changed")
                    except Exception:
                        _structure_refuse(self, "publication_final_source_or_layout_changed")
                return
            key = id(frame)
            current = thread.frames.get(key)
            if event == "call" and current is not None:
                if current.pending_generation is not None:
                    current.delivery_failed = True
                    _structure_refuse(self, "lifetime_resume_before_native_delivery")
                    return
                if not current.suspended:
                    _structure_refuse(self, "active_frame_identity_reentered")
                current.suspended = False
                current.resumptions += 1
                if role == "clock":
                    thread.clocks.append(key)
                return
            if event == "return":
                if current is None:
                    _structure_refuse(self, "terminal_without_original_entry")
                    return
                if role in _STRUCTURE_LIFETIME_ROLES:
                    self._structure_pending_return(frame, thread, current)
                    return
                instruction = _STRUCTURE_SITES[frame.f_code].get(frame.f_lasti)
                outcome = (
                    "yield"
                    if instruction == "YIELD_VALUE"
                    else "unknown"
                    if instruction is None
                    else "returned"
                    if instruction in ("RETURN_VALUE", "RETURN_CONST")
                    else "unwound"
                )
                if outcome == "unknown":
                    _structure_refuse(self, "unknown_original_terminal_site")
                self._structure_return(thread, key, current, outcome, arg)
                return
            # Actual original body entry, before source/carrier attribution.
            memo = _CATALOG_STAGE_CONTEXT.get()
            clock = thread.frames.get(thread.clocks[-1]) if thread.clocks else self._structure_memo_clocks.get(id(memo))
            phase = clock.phase if clock is not None else thread.worker_phase
            clock_id = clock.event if clock is not None else thread.worker_clock
            if phase == "outside_root" and any(owner.role in ("root", "clock") for owner in thread.frames.values()):
                phase = "unphased_root"
            owner = _StructureInvocation(
                role, 0, 0, phase, clock_id, clock.stage if clock else None, clock.memo if clock else thread.expected, None
            )
            if frame.f_globals is not next(function.__globals__ for function, label in _STRUCTURE_ROLES if label == role):
                _structure_refuse(self, "original_globals_changed")
            if role == "clock":
                phase = frame.f_locals.get("stage")
                if type(phase) is not str or phase not in ("physical", "retention", "guard", "quota"):
                    _structure_refuse(self, "unknown_clock_phase")
                    phase = "unphased_root"
                owner.phase = phase
                owner.memo = owner.stage = None
                attempt = _structure_emit(self, thread, "clock_attempt", owner=owner, source=role, phase=phase)
                instance = frame.f_locals.get("self")
                if type(instance) is not _STRUCTURE_CLOCK_CLASS or "run" in instance.__dict__:
                    _structure_refuse(self, "clock_instance_or_dispatch_unowned")
                previous = thread.last.get(("clock_handoff", 0))
                if previous is not None:
                    thread.edges.append(StructureEdge(previous, attempt.ordinal, "handoff"))
                owner.clock = attempt.ordinal
                thread.events[-1] = replace(attempt, clock=attempt.ordinal)
                thread.clocks.append(key)
            elif role in ("root", "handoff_root", "submit", "finish"):
                owner.event = _structure_number(thread)
                if role == "finish":
                    parent = frame.f_back
                    for _ in range(64):
                        if parent is None or parent.f_code is _STRUCTURE_HANDOFF_ROOT.__code__:
                            break
                        parent = parent.f_back
                    else:
                        _structure_refuse(self, "handoff_parent_search_exhausted")
                    if parent is not None and parent.f_code is _STRUCTURE_HANDOFF_ROOT.__code__:
                        owner.parent = id(parent)
                    elif any(active.role == "handoff_root" and active.clock == clock_id for active in thread.frames.values()):
                        _structure_refuse(self, "handoff_parent_unbound")
                thread.frames[key] = owner
                return
            elif role == "executor":
                caller = frame.f_back
                args = frame.f_locals.get("args")
                function = frame.f_locals.get("fn")
                if (
                    caller is None
                    or caller.f_code is not _STRUCTURE_SUBMIT.__code__
                    or type(args) is not tuple
                    or len(args) != 1
                    or args[0] is not caller.f_locals.get("callable_")
                ):
                    _structure_refuse(self, "executor_without_original_submission")
                    thread.frames[key] = owner
                    return
                if (
                    type(function) is not MethodType
                    or function.__func__ is not _STRUCTURE_WITNESS
                    or type(function.__self__) is not RequiredInvocationWitness
                ):
                    _structure_refuse(self, "executor_witness_changed")
                    thread.frames[key] = owner
                    return
                witness = function.__self__
                finish = caller
                for _ in range(64):
                    if finish is None or finish.f_code is _STRUCTURE_FINISH.__code__:
                        break
                    finish = finish.f_back
                if finish is None:
                    _structure_refuse(self, "submission_finish_source_unbound")
                attempt = _structure_emit(self, thread, "submission_attempt", owner=owner, source="original_executor_submit")
                thread.events[-1] = replace(attempt, submission=attempt.ordinal)
                if finish is not None and id(finish) in thread.frames:
                    finish_owner = thread.frames[id(finish)]
                    finish_owner.event = attempt.ordinal
                    finish_owner.submission = attempt.ordinal
                    previous = thread.last.get(("handoff_root", finish_owner.parent)) if finish_owner.parent is not None else None
                    if previous is not None:
                        thread.edges.append(StructureEdge(previous, attempt.ordinal, "handoff"))
                if id(witness) in self._structure_submissions:
                    _structure_refuse(self, "concurrent_witness_identity")
                self._structure_submissions[id(witness)] = (
                    attempt.ordinal,
                    phase,
                    clock_id,
                    id(memo) if memo is not None else None,
                    id(finish),
                )
            else:
                window = next(
                    (
                        thread.frames[identifier]
                        for identifier in reversed(thread.windows)
                        if identifier in thread.frames and thread.frames[identifier].clock == clock_id
                    ),
                    None,
                )
                if role == "window":
                    engine = frame.f_locals.get("engine")
                    owner.engine = next((index for index, known in enumerate(self._structure_engines, 1) if engine is known), None)
                    if owner.engine is None:
                        _structure_refuse(self, "window_engine_unowned")
                    attempt = _structure_emit(self, thread, "window_attempt", owner=owner, source=role, engine=owner.engine)
                    thread.windows.append(key)
                else:
                    conn = frame.f_locals.get("self" if role in ("sql", "driver") else "conn")
                    if type(conn) is not Connection:
                        _structure_refuse(self, "connection_source_unowned")
                    else:
                        owner.connection = id(conn)
                        owner.engine = next((index for index, known in enumerate(self._structure_engines, 1) if conn.engine is known), None)
                        if owner.engine is None or "execute" in conn.__dict__ or "exec_driver_sql" in conn.__dict__:
                            _structure_refuse(self, "connection_dispatch_unowned")
                    owner.parent = window.event if window is not None else None
                    if phase not in ("outside_root",) and window is None:
                        _structure_refuse(self, "actual_work_without_original_window")
                    if phase == "unphased_root":
                        _structure_refuse(self, "actual_work_without_original_clock")
                    if window is not None and window.connection is not None and window.connection != owner.connection:
                        _structure_refuse(self, "connection_changed_inside_window")
                    kind = "sql_attempt" if role in ("sql", "driver") else role + "_attempt"
                    attempt = _structure_emit(
                        self, thread, kind, owner=owner, source=role, engine=owner.engine, window=owner.parent, connection=owner.connection
                    )
                    if role in ("sql", "driver"):
                        statement = frame.f_locals.get("statement")
                        owner.statement = id(statement)
                        if role == "sql":
                            owner.store_role = _structure_store_role(frame, statement)
                            if owner.store_role is not None:
                                owner.store_thread = identity
                                owner.store_submission = thread.submission
                                try:
                                    owner.store_source_shape = _structure_store_source_shape(statement, owner.store_role)[0]
                                except Exception:
                                    _structure_refuse(self, "publication_original_source_shape_unbound")
                            elif type(statement) in (Insert, Update):
                                _structure_refuse(self, "publication_without_original_producer_callsite")
                        if role == "driver":
                            caller = frame.f_back
                            owner.driver_role = next(
                                (
                                    label
                                    for code, literal, label in _STRUCTURE_DRIVER_LITERALS
                                    if caller is not None and caller.f_code is code and statement is literal
                                ),
                                None,
                            )
                            if owner.driver_role is None:
                                _structure_refuse(self, "driver_sql_without_fixed_original_source")
            owner.event, owner.index = attempt.ordinal, len(thread.events) - 1
            thread.frames[key] = owner
        except Exception as error:
            # No application/observer lock and no native error payload retained.
            _structure_refuse(self, "structure_profile_internal_error")
            try:
                _structure_internal_error(self, error)
            except BaseException:
                self._structure_observer_incomplete = True

    def _structure_carrier(self) -> tuple[int | None, int | None, bool | None]:
        memo = _CATALOG_STAGE_CONTEXT.get()
        if memo is None:
            return None, None, None
        stage = next((index for index, known in enumerate(self.memos, 1) if memo is known), None)
        if type(memo) is not RetentionStageMemo or stage is None:
            _structure_refuse(self, "lifetime_carrier_unowned")
            return id(memo), stage, None
        closed = memo._closed
        if type(closed) is not bool:
            _structure_refuse(self, "lifetime_carrier_closed_unowned")
            return id(memo), stage, None
        return id(memo), stage, closed

    def _structure_row_code_facts(self, code: CodeType) -> _StructureRowCodeFacts | None:
        probe = _StructureRowCodeIdentity(code)
        existing = self._structure_row_codes.get(probe)
        if existing is not None:
            if existing.anchor() is not code:
                _structure_refuse(self, "publication_row_code_facts_anchor_changed")
                return None
            return existing
        owner_ref = ref(self)

        def evict(anchor):
            owner = owner_ref()
            if owner is not None and anchor() is None:
                # The dead key equals only itself. A recycled address or an
                # equal-looking CodeType cannot remove another live key.
                owner._structure_row_codes.pop(anchor, None)

        anchor = _StructureRowCodeIdentity(code, evict)
        facts = _StructureRowCodeFacts(
            anchor,
            code.co_varnames[: code.co_argcount],
            code.co_varnames[code.co_argcount + code.co_kwonlyargcount] if code.co_flags & CO_VARARGS else None,
            bool(code.co_flags & (CO_GENERATOR | CO_COROUTINE | CO_ASYNC_GENERATOR)),
            frozenset(item.offset for item in dis.get_instructions(code) if item.opname in ("RETURN_VALUE", "RETURN_CONST")),
        )
        self._structure_row_codes[anchor] = facts
        return facts

    @staticmethod
    def _structure_row_request(thread: _StructureThread, frame: FrameType | None) -> tuple[int, _StructureInvocation] | None:
        for _ in range(64):
            if frame is None:
                break
            owner = thread.frames.get(id(frame))
            if owner is not None and owner.role in ("sql", "driver"):
                return id(frame), owner
            frame = frame.f_back
        return None

    @staticmethod
    def _structure_row_live(owner: _StructureInvocation, caller: FrameType) -> bool:
        origin = owner.row_origin
        values = caller.f_locals
        context, parameters, row = values.get("context"), values.get("parameters"), values.get("effective_parameters")
        return (
            origin is not None
            and origin.code == id(_STRUCTURE_EXECUTION_INIT.__code__)
            and origin.globals == id(_STRUCTURE_EXECUTION_INIT.__globals__)
            and id(caller) == owner.row_dispatch
            and caller.f_code is _STRUCTURE_SINGLE_CONTEXT.__code__
            and caller.f_globals is _STRUCTURE_SINGLE_CONTEXT.__globals__
            and type(context) in (SQLiteExecutionContext, PGExecutionContext_psycopg)
            and id(context) == origin.context == owner.execution_context
            and id(context.root_connection) == owner.connection
            and values.get("self") is context.root_connection
            and values.get("cursor") is context.cursor
            and id(context.cursor) == origin.cursor
            and context.executemany is False
            and type(parameters) is list
            and id(parameters) == origin.container
            and context.parameters is parameters
            and len(parameters) == 1
            and row is parameters[0]
            and id(row) == origin.row
        )

    def _structure_row_source(
        self, thread: _StructureThread, owner: _StructureInvocation, edge: _StructureRowCallback, frame: FrameType
    ) -> bool:
        caller = frame.f_back
        if caller is None:
            return False
        callback = caller.f_locals.get("fn")
        receiver = callback.__self__ if type(callback) is MethodType else None
        function = callback.__func__ if type(callback) is MethodType else callback
        return (
            type(function) is FunctionType
            and id(callback) == edge.function
            and (None if receiver is None else id(receiver)) == edge.receiver
            and frame.f_code is function.__code__
            and frame.f_globals is function.__globals__
            and id(frame) == edge.frame
            and id(frame.f_code) == edge.code
            and id(frame.f_globals) == edge.globals
            and caller.f_code is _STRUCTURE_SINGLE_CONTEXT.__code__
            and caller.f_globals is _STRUCTURE_SINGLE_CONTEXT.__globals__
            and caller.f_lasti in _STRUCTURE_ROW_CALL_OFFSETS
            and edge.call_offset == _STRUCTURE_ROW_CALL_OFFSET
            and id(caller) == edge.dispatch == owner.row_dispatch
            and thread.frames.get(edge.request) is owner
            and owner.event == edge.event
            and owner.row_callback is edge
            and owner.row_origin is edge.origin
            and owner.row_generation == edge.generation
            and thread.identity == get_ident() == edge.thread
            and edge.hook == id(sys.getprofile())
            and self._structure_exchange_supported()
            and self._structure_carrier() == edge.carrier
            and (thread.submission, thread.expected, thread.witness) == (edge.submission, edge.expected, edge.witness)
        )

    @staticmethod
    def _structure_row_arguments(frame: FrameType, caller: FrameType, callback: object, facts: _StructureRowCodeFacts) -> bool:
        bound = type(callback) is MethodType
        function = callback.__func__ if bound else callback
        if type(function) is not FunctionType or facts.anchor() is not frame.f_code or facts.unsupported:
            return False
        current = caller.f_locals
        arguments = (
            current.get("self"),
            current.get("cursor"),
            current.get("str_statement"),
            current.get("effective_parameters"),
            current.get("context"),
            current["context"].executemany,
        )
        if bound:
            arguments = (callback.__self__, *arguments)
        values = frame.f_locals
        positional = len(facts.positional_names)
        if any(values.get(name) is not value for name, value in zip(facts.positional_names, arguments, strict=False)):
            return False
        if len(arguments) <= positional:
            return True
        if facts.vararg_name is None:
            return False
        extra = values.get(facts.vararg_name)
        return (
            type(extra) is tuple
            and len(extra) == len(arguments) - positional
            and all(actual is expected for actual, expected in zip(extra, arguments[positional:], strict=True))
        )

    def _structure_row_consume(self, thread: _StructureThread, owner: _StructureInvocation, caller: FrameType) -> None:
        edge = owner.row_callback
        if edge is None:
            if owner.row_callbacks_started == 0:
                _structure_refuse(self, "publication_row_callback_missing")
            return
        if (
            not edge.returned
            or edge.outcome != "returned"
            or edge.generation != owner.row_generation
            or edge.origin is not owner.row_origin
            or thread.row_returns.get(edge.generation) is not edge
            or thread.frames.get(edge.request) is not owner
            or edge.event != owner.event
            or edge.thread != get_ident()
            or edge.hook != id(sys.getprofile())
            or edge.dispatch != id(caller)
            or not self._structure_row_live(owner, caller)
            or edge.carrier != self._structure_carrier()
            or (thread.submission, thread.expected, thread.witness) != (edge.submission, edge.expected, edge.witness)
        ):
            _structure_refuse(self, "publication_row_callback_not_consumed")
        thread.row_callbacks.pop(edge.frame, None)
        owner.row_callbacks_closed += 1
        owner.row_callback = None

    def _structure_row_profile(self, frame: FrameType, event: str, arg: object, thread: _StructureThread) -> None:
        if event not in ("call", "c_call", "return"):
            return
        # Audit only active listener frames, borrowing their natural live caller
        # aliases. Numeric pending receipts never keep a retired row/frame alive.
        if thread.row_callbacks and event in ("call", "c_call"):
            ancestor = frame
            remaining = len(thread.row_callbacks)
            for _ in range(64):
                if ancestor is None:
                    break
                edge = thread.row_callbacks.get(id(ancestor))
                if edge is not None and id(ancestor.f_code) == edge.code and id(ancestor.f_globals) == edge.globals:
                    owner = thread.frames.get(edge.request)
                    if owner is None or not self._structure_row_source(thread, owner, edge, ancestor):
                        _structure_refuse(self, "publication_row_callback_source_changed")
                    remaining -= 1
                    if remaining == 0:
                        break
                ancestor = ancestor.f_back
        if frame.f_code is _STRUCTURE_SINGLE_CONTEXT.__code__:
            found = self._structure_row_request(thread, frame.f_back)
            if found is None or found[1].store_role is None:
                return
            _key, owner = found
            if event == "call":
                context = frame.f_locals.get("context")
                origin = owner.row_origin
                if (
                    owner.row_dispatch is not None
                    or origin is None
                    or frame.f_globals is not _STRUCTURE_SINGLE_CONTEXT.__globals__
                    or type(context) not in (SQLiteExecutionContext, PGExecutionContext_psycopg)
                    or id(context) != origin.context
                    or id(context.root_connection) != owner.connection
                    or frame.f_locals.get("self") is not context.root_connection
                    or id(context.parameters) != origin.container
                    or type(context.parameters) is not list
                    or len(context.parameters) != 1
                    or id(context.parameters[0]) != origin.row
                    or id(context.cursor) != origin.cursor
                ):
                    _structure_refuse(self, "publication_row_dispatch_origin_changed")
                owner.row_dispatch = id(frame)
            elif event == "c_call" and frame.f_lasti in _STRUCTURE_ROW_CALL_OFFSETS:
                _structure_refuse(self, "publication_row_callback_callable_unsupported")
            elif event == "return":
                edge = owner.row_callback
                if edge is not None and edge.returned and edge.outcome == "unwound":
                    if (
                        edge.generation != owner.row_generation
                        or edge.origin is not owner.row_origin
                        or thread.row_returns.get(edge.generation) is not edge
                        or thread.frames.get(edge.request) is not owner
                        or edge.event != owner.event
                        or edge.thread != get_ident()
                        or edge.hook != id(sys.getprofile())
                        or edge.carrier != self._structure_carrier()
                        or (thread.submission, thread.expected, thread.witness) != (edge.submission, edge.expected, edge.witness)
                        or not self._structure_row_live(owner, frame)
                    ):
                        _structure_refuse(self, "publication_row_callback_unwind_changed")
                    thread.row_callbacks.pop(edge.frame, None)
                    owner.row_callbacks_closed += 1
                    owner.row_callback = None
                elif edge is not None:
                    self._structure_row_consume(thread, owner, frame)
                if id(frame) != owner.row_dispatch or owner.row_dispatch_closed:
                    _structure_refuse(self, "publication_row_dispatch_terminal_changed")
                owner.row_dispatch_closed = True
            return
        caller = frame.f_back
        if caller is None or caller.f_code is not _STRUCTURE_SINGLE_CONTEXT.__code__:
            return
        if caller.f_lasti not in _STRUCTURE_ROW_CALL_OFFSETS and caller.f_lasti != _STRUCTURE_ROW_UNPACK_OFFSET:
            return
        found = self._structure_row_request(thread, caller.f_back)
        if found is None or found[1].store_role is None:
            return
        key, owner = found
        if caller.f_lasti == _STRUCTURE_ROW_UNPACK_OFFSET:
            _structure_refuse(self, "publication_row_callback_callable_unsupported")
            return
        if event == "call":
            if owner.row_callback is not None:
                self._structure_row_consume(thread, owner, caller)
            callback = caller.f_locals.get("fn")
            function = callback.__func__ if type(callback) is MethodType else callback
            if (
                type(function) is not FunctionType
                or frame.f_code is not function.__code__
                or frame.f_globals is not function.__globals__
                or not self._structure_row_live(owner, caller)
            ):
                _structure_refuse(self, "publication_row_callback_entry_changed")
                return
            facts = self._structure_row_code_facts(frame.f_code)
            if facts is None:
                return
            if not self._structure_row_arguments(frame, caller, callback, facts):
                _structure_refuse(self, "publication_row_callback_entry_changed")
                return
            thread.row_generation += 1
            owner.row_generation = thread.row_generation
            owner.row_callbacks_started += 1
            edge = _StructureRowCallback(
                thread.row_generation,
                key,
                owner.event,
                owner.row_origin,
                id(caller),
                _STRUCTURE_ROW_CALL_OFFSET,
                id(frame),
                id(callback),
                id(callback.__self__) if type(callback) is MethodType else None,
                id(frame.f_code),
                id(frame.f_globals),
                get_ident(),
                id(sys.getprofile()),
                self._structure_carrier(),
                thread.submission,
                thread.expected,
                thread.witness,
                facts.return_sites,
            )
            owner.row_callback = edge
            thread.row_callbacks[id(frame)] = edge
        elif event == "return":
            edge = owner.row_callback
            if edge is None or edge.returned:
                _structure_refuse(self, "publication_row_callback_return_unpaired")
                return
            if not self._structure_row_source(thread, owner, edge, frame):
                _structure_refuse(self, "publication_row_callback_return_source_changed")
            edge = replace(edge, returned=True, outcome="returned" if frame.f_lasti in edge.return_sites else "unwound")
            owner.row_callback = edge
            if edge.generation in thread.row_returns:
                _structure_refuse(self, "publication_row_callback_receipt_reused")
            else:
                thread.row_returns[edge.generation] = edge
            thread.row_callbacks.pop(edge.frame, None)
            if edge.outcome == "returned" and (
                type(arg) is not tuple
                or len(arg) != 2
                or not self._structure_row_live(owner, caller)
                or arg[1] is not caller.f_locals.get("effective_parameters")
            ):
                _structure_refuse(self, "publication_row_callback_return_changed")

    def _structure_pending_return(self, frame: FrameType, thread: _StructureThread, owner: _StructureInvocation) -> None:
        key = id(frame)
        if key in thread.pending_returns or owner.pending_generation is not None:
            owner.delivery_failed = True
            _structure_refuse(self, "lifetime_profile_return_overlapped")
            return
        monitor = self._structure_lifetime_monitor
        if monitor is None or not monitor.current():
            owner.delivery_failed = True
            _structure_refuse(self, "lifetime_monitor_not_owned")
            return
        hook = sys.getprofile()
        if type(hook) is not FunctionType or not any(hook is known for known in self._owned_hooks):
            owner.delivery_failed = True
            _structure_refuse(self, "lifetime_profile_hook_unowned")
            return
        thread.return_generation += 1
        owner.pending_generation = thread.return_generation
        thread.pending_returns[key] = _StructurePendingReturn(
            thread.return_generation,
            key,
            id(frame.f_code),
            id(frame.f_globals),
            id(owner),
            owner.event,
            owner.role,
            get_ident(),
            id(hook),
            self._structure_carrier(),
            thread.submission,
            thread.expected,
            thread.witness,
        )

    def _structure_lifetime_event(self, frame: FrameType, kind: str, code: CodeType, offset: int, value: object) -> None:
        if self._structure_closed or self._structure_closing:
            _structure_refuse(self, "lifetime_delivery_after_close")
            return
        if type(frame) is not FrameType or type(code) is not CodeType or type(kind) is not str or type(offset) is not int:
            _structure_refuse(self, "lifetime_native_input_unowned")
            return
        identity = get_ident()
        thread = self._structure_threads.get(identity)
        if thread is None:
            _structure_refuse(self, "lifetime_native_thread_unowned")
            return
        key = id(frame)
        pending = thread.pending_returns.get(key)
        owner = thread.frames.get(key)
        source = next(
            (function for function, role in _STRUCTURE_ROLES if role in _STRUCTURE_LIFETIME_ROLES and function.__code__ is code), None
        )
        if pending is None or owner is None:
            _structure_refuse(self, "lifetime_native_without_profile_return")
            return
        monitor = self._structure_lifetime_monitor
        entry = thread.native_entry
        if (
            type(entry) is not _StructureNativeEntry
            or entry.kind != kind
            or entry.frame != key
            or entry.code != id(code)
            or entry.globals != id(frame.f_globals)
            or entry.offset != offset
            or monitor is None
            or not any(
                entry.callback == id(callback) and actual == kind
                for (_event, callback), actual in zip(monitor.deliveries, ("yield", "returned", "unwound"), strict=True)
            )
        ):
            owner.delivery_failed = True
            _structure_refuse(self, "lifetime_native_entry_unowned")
            return
        if (
            entry.invocation != id(owner)
            or entry.generation != pending.generation
            or entry.event != owner.event
            or entry.thread != identity
        ):
            owner.delivery_failed = True
            _structure_refuse(self, "lifetime_native_provenance_changed")
            return
        hook = sys.getprofile()
        if type(hook) is not FunctionType or not any(hook is known for known in self._owned_hooks):
            owner.delivery_failed = True
            _structure_refuse(self, "lifetime_profile_hook_unowned")
            return
        if (
            monitor is None
            or not monitor.current()
            or not self._structure_sources_current()
            or source is None
            or frame.f_code is not code
            or frame.f_globals is not source.__globals__
            or pending.frame != key
            or pending.code != id(code)
            or pending.globals != id(frame.f_globals)
            or pending.thread != identity
            or pending.invocation != id(owner)
            or pending.event != owner.event
            or pending.role != owner.role
            or pending.generation != owner.pending_generation
            or owner.delivery_failed
            or pending.hook != id(hook)
            or pending.carrier != self._structure_carrier()
            or pending.submission != thread.submission
            or pending.expected != thread.expected
            or pending.witness != thread.witness
        ):
            owner.delivery_failed = True
            _structure_refuse(self, "lifetime_native_provenance_changed")
            return
        instruction = _STRUCTURE_SITES[code].get(offset)
        if (
            (kind == "yield" and instruction != "YIELD_VALUE")
            or (kind == "returned" and instruction not in ("RETURN_VALUE", "RETURN_CONST"))
            or kind not in ("yield", "returned", "unwound")
            or instruction is None
        ):
            owner.delivery_failed = True
            _structure_refuse(self, "lifetime_native_site_unbound")
            return
        thread.pending_returns.pop(key)
        owner.pending_generation = None
        # No fabricated profile event; the positive native kind owns this update.
        self._structure_return(thread, key, owner, kind, None if kind == "unwound" else value)

    def _structure_return(self, thread: _StructureThread, key: int, current: _StructureInvocation, outcome: str, arg: object) -> None:
        identity = get_ident()
        role = current.role
        if outcome == "yield":
            current.suspended = True
            current.suspensions += 1
            if role == "clock" and (not thread.clocks or thread.clocks.pop() != key):
                _structure_refuse(self, "clock_suspension_order")
            if role == "window":
                if current.suspensions != 1 or type(arg) is not Connection:
                    _structure_refuse(self, "window_ready_shape")
                else:
                    current.connection = id(arg)
                    current.engine = next((index for index, engine in enumerate(self._structure_engines, 1) if arg.engine is engine), None)
                    claimed = self._structure_connections.get(id(arg))
                    if claimed is not None and claimed != (current.event, identity):
                        _structure_refuse(self, "concurrent_connection_identity")
                    self._structure_connections[id(arg)] = (current.event, identity)
                    _structure_emit(
                        self,
                        thread,
                        "window_ready",
                        owner=current,
                        source="window",
                        window=current.event,
                        engine=current.engine,
                        connection=id(arg),
                    )
            return
        if current.suspended or current.suspensions != current.resumptions:
            _structure_refuse(self, "unbalanced_original_lifetime")
        thread.frames.pop(key)
        if role == "clock" and (not thread.clocks or thread.clocks.pop() != key):
            _structure_refuse(self, "clock_terminal_order")
        if role == "window":
            if key in thread.windows:
                thread.windows.remove(key)
            if current.connection is not None:
                self._structure_connections.pop(current.connection, None)
        if role in ("clock", "window", "finish"):
            lifetime_owner = current.submission if role == "finish" else current.event
            if lifetime_owner is None:
                _structure_refuse(self, "lifetime_without_public_submission")
            else:
                thread.lifetimes.append(
                    StructureLifetime(
                        "submission" if role == "finish" else role, lifetime_owner, current.suspensions, current.resumptions, outcome
                    )
                )
        if role in ("submit", "executor", "root", "handoff_root"):
            if role == "handoff_root":
                thread.last.pop(("handoff_root", key), None)
            return
        if role == "finish":
            submissions = [current.submission] if current.submission is not None else []
            if not submissions:
                _structure_refuse(self, "join_without_actual_submission")
            result_kind = (
                "required_sql_returned"
                if type(arg) is RequiredSQLReturned
                else "required_sql_raised"
                if type(arg) is RequiredSQLRaised
                else None
            )
            if outcome == "returned" and result_kind is None:
                _structure_refuse(self, "join_outcome_type_unowned")
            for submission in submissions:
                joined = _structure_emit(
                    self,
                    thread,
                    "submission_join",
                    owner=current,
                    source="original_finish_once",
                    submission=submission,
                    outcome=outcome,
                    result_kind=result_kind,
                )
                terminals = [
                    row
                    for buffer in self._structure_threads.values()
                    for row in buffer.events
                    if row.kind == "worker_terminal" and row.submission == submission
                ]
                if len(terminals) != 1:
                    _structure_refuse(self, "join_without_one_worker_terminal")
                else:
                    thread.edges.append(StructureEdge(terminals[0].ordinal, joined.ordinal, "worker_to_join"))
                if current.parent is not None:
                    thread.last[("handoff_root", current.parent)] = joined.ordinal
            return
        if role in ("sql", "driver"):
            if current.store_role is not None and (
                current.row_origin is None
                or current.row_dispatch is None
                or not current.row_dispatch_closed
                or current.row_callback is not None
                or current.row_callbacks_started == 0
                or current.row_callbacks_closed != current.row_callbacks_started
            ):
                _structure_refuse(self, "publication_row_observation_incomplete")
            if (
                not current.listener_seen
                or current.driver_attempts != 1
                or current.driver_outcome not in ("returned", "unwound")
                or (current.driver_outcome == "returned" and not current.listener_returned)
            ):
                _structure_refuse(self, "raw_sql_without_complete_listener")
            _structure_emit(
                self,
                thread,
                "sql_return",
                owner=current,
                source=current.driver_source or role,
                window=current.parent,
                engine=current.engine,
                connection=current.connection,
                outcome=outcome,
                sql=thread.events[current.index].sql,
            )
            return
        result_kind = None
        if outcome == "returned":
            if role == "db_clock":
                if type(arg) is not datetime or arg.tzinfo is None:
                    _structure_refuse(self, "database_clock_without_aware_datetime")
                else:
                    result_kind = "datetime"
            elif role == "admission":
                if type(arg) is not bool:
                    _structure_refuse(self, "admission_without_boolean")
                else:
                    result_kind = "bool_true" if arg else "bool_false"
            elif role == "clock":
                result_kind = "none" if arg is None else "int" if type(arg) is int else "owned_result"
        terminal = _structure_emit(
            self,
            thread,
            role + "_terminal",
            owner=current,
            source=role,
            clock=current.event if role == "clock" else current.clock,
            window=current.event if role == "window" else None,
            engine=current.engine,
            connection=current.connection,
            outcome=outcome,
            result_kind=result_kind,
        )
        if role == "clock":
            thread.last[("clock_handoff", 0)] = terminal.ordinal
        return

    def _structure_before_cursor_execute(self, conn, cursor, statement, parameters, context, executemany) -> None:
        if self._structure_closed or self._structure_closing:
            return
        try:
            thread = self._structure_threads.get(get_ident())
            if thread is None:
                _structure_refuse(self, "listener_thread_unowned")
                return
            frame = sys._getframe().f_back
            owner = None
            for _ in range(64):
                if frame is None:
                    break
                candidate = thread.frames.get(id(frame))
                if candidate is not None and candidate.role in ("sql", "driver"):
                    owner = candidate
                    break
                frame = frame.f_back
            if owner is None:
                _structure_refuse(self, "listener_without_original_execution")
                return
            if owner.listener_seen or owner.connection != id(conn) or type(conn) is not Connection:
                _structure_refuse(self, "execution_listener_pair_changed")
                return
            owner.listener_seen = True
            if (
                type(context) not in (SQLiteExecutionContext, PGExecutionContext_psycopg)
                or id(context) != owner.execution_context
                or context.root_connection is not conn
            ):
                _structure_refuse(self, "execution_context_unowned")
                return
            if _structure_sql_signature(statement) != owner.sql_signature or (
                statement is not context.statement and _structure_sql_signature(context.statement) != owner.sql_signature
            ):
                _structure_refuse(self, "before_listener_statement_changed")
                return
            if type(executemany) is not bool or owner.pending_shape is None or owner.pending_shape.executemany is not executemany:
                _structure_refuse(self, "before_listener_shape_or_mode_changed")
        except Exception:
            if self._observation_incomplete:
                raise
            _structure_refuse(self, "unsupported_or_unbound_SQL_listener")

    def _structure_after_cursor_execute(self, conn, cursor, statement, parameters, context, executemany) -> None:
        if self._structure_closed or self._structure_closing:
            return
        thread = self._structure_threads.get(get_ident())
        if thread is None:
            _structure_refuse(self, "listener_return_thread_unowned")
            return
        frame = sys._getframe().f_back
        for _ in range(64):
            if frame is None:
                break
            owner = thread.frames.get(id(frame))
            if owner is not None and owner.role in ("sql", "driver"):
                if owner.listener_returned or not owner.listener_seen or owner.connection != id(conn):
                    _structure_refuse(self, "listener_return_pair_changed")
                owner.listener_returned = True
                return
            frame = frame.f_back
        _structure_refuse(self, "listener_return_without_original_execution")

    def _structure_transaction_attempt(self, conn, kind: Literal["begin", "commit", "rollback"]) -> None:
        if self._structure_closed or self._structure_closing:
            return
        thread = self._structure_threads.get(get_ident())
        if thread is None or type(conn) is not Connection:
            _structure_refuse(self, "transaction_thread_or_connection_unowned")
            return
        memo = _CATALOG_STAGE_CONTEXT.get()
        clock = thread.frames.get(thread.clocks[-1]) if thread.clocks else self._structure_memo_clocks.get(id(memo))
        clock_id = clock.event if clock else thread.worker_clock
        window = next(
            (thread.frames[key] for key in reversed(thread.windows) if key in thread.frames and thread.frames[key].clock == clock_id), None
        )
        engine = next((index for index, known in enumerate(self._structure_engines, 1) if conn.engine is known), None)
        if window is None and clock_id is not None:
            _structure_refuse(self, "transaction_without_original_window")
        _structure_emit(
            self,
            thread,
            "transaction_" + kind + "_attempt",
            owner=window or clock,
            source="engine_event",
            engine=engine,
            window=window.event if window else None,
            connection=id(conn),
        )

    def _structure_close(self, primary: BaseException | None) -> None:
        if not self._structure_required:
            return
        if self._structure_closed:
            # A worker can close the observation, but only the original loop
            # owner completes deferred global monitoring cleanup. The ledger
            # stays closed/incomplete; this never repairs missed observation.
            if get_ident() == self._owner_thread and self._structure_lifetime_monitor is not None:
                self._structure_lifetime_monitor.close(primary)
            return
        self._structure_closing = True
        interrupt = None
        if any(
            thread.frames
            or thread.pending_returns
            or thread.row_callbacks
            or thread.native_entry is not None
            or thread.submission is not None
            for thread in self._structure_threads.values()
        ):
            _structure_refuse(self, "closed_with_active_original_owner")
        try:
            if not self._structure_sources_current():
                _structure_refuse(self, "original_source_changed_at_close")
        except Exception as error:
            _structure_refuse(self, "original_source_unavailable_at_close")
            if self._observation_incomplete:
                interrupt = error
        except BaseException as error:
            _structure_refuse(self, "source_audit_interrupted_at_close")
            interrupt = error
        if self._structure_lifetime_monitor is not None and get_ident() == self._owner_thread:
            try:
                self._structure_lifetime_monitor.close(primary)
            except BaseException as error:
                _structure_refuse(self, "lifetime_monitor_close_failed")
                if interrupt is None:
                    interrupt = error
        for engine, name, callback in reversed(self._structure_listeners):
            try:
                if sql_events.contains(engine, name, callback):
                    sql_events.remove(engine, name, callback)
                if sql_events.contains(engine, name, callback):
                    _structure_refuse(self, "owned_listener_survived_cleanup")
            except Exception as error:
                _structure_refuse(self, "owned_listener_cleanup_failed")
                if self._observation_incomplete and interrupt is None:
                    interrupt = error
            except BaseException as error:
                _structure_refuse(self, "owned_listener_cleanup_interrupted")
                if interrupt is None:
                    interrupt = error
        self._structure_closed = True
        self._structure_closing = False
        self._structure_observation_verified = False
        for thread in self._structure_threads.values():
            for owner in tuple(thread.frames.values()):
                owner.sql_signature = None
                owner.execution_context = None
                owner.pending_shape = None
                owner.row_origin = None
        if interrupt is not None and primary is None:
            raise interrupt

    def structure_snapshot(self) -> StructureSnapshot:
        events = tuple(
            sorted((event for thread in self._structure_threads.values() for event in thread.events), key=lambda event: event.ordinal)
        )
        edges = tuple(
            sorted(
                (edge for thread in self._structure_threads.values() for edge in thread.edges),
                key=lambda edge: (edge.before, edge.after, edge.kind),
            )
        )
        lifetimes = tuple(
            sorted(
                (lifetime for thread in self._structure_threads.values() for lifetime in thread.lifetimes),
                key=lambda lifetime: lifetime.owner,
            )
        )
        error_count = self._structure_error_count + sum(thread.error_count for thread in self._structure_threads.values())
        errors = tuple(self._structure_errors + [code for thread in self._structure_threads.values() for code in thread.errors])[:8]
        complete = (
            self._structure_required
            and self._structure_closed
            and not error_count
            and not self._structure_observer_incomplete
            and not self._observation_incomplete
            and not self.errors
            and any(event.kind == "clock_attempt" for event in events)
        )
        return StructureSnapshot(
            1,
            self._structure_entrypoint,
            self._structure_required,
            self._structure_closed,
            complete,
            self._structure_observation_verified,
            errors,
            events,
            edges,
            lifetimes,
        )

    def _structure_state(self) -> tuple[object, ...]:
        return (
            self.structure_snapshot(),
            self._structure_error_count + sum(thread.error_count for thread in self._structure_threads.values()),
            self._structure_observer_incomplete,
            tuple(
                (
                    thread.identity,
                    thread.row_generation,
                    tuple((generation, id(receipt), receipt) for generation, receipt in thread.row_returns.items()),
                    tuple((frame, id(receipt), receipt) for frame, receipt in thread.row_callbacks.items()),
                )
                for thread in self._structure_threads.values()
            ),
        )

    def _assert_structure_observation(self, requested: bool) -> None:
        assert type(requested) is bool, "structure observation assertion requires explicit boolean"
        if not self._structure_required:
            assert not requested, "structure observation requires explicit mode"
            return
        assert requested, "structure observation assertion is required"
        snapshot = self.structure_snapshot()
        assert snapshot.closed and snapshot.complete and not self._observation_incomplete, "structure observation incomplete" + (
            "\n" + ",".join(snapshot.errors) if snapshot.errors else ""
        )
        self._structure_observation_verified = True

    def assert_structure_matches(self, other: StructureSnapshot) -> None:
        self._structure_compare(self.structure_snapshot(), other)

    @staticmethod
    def _structure_compare(left: StructureSnapshot, right: StructureSnapshot) -> None:
        assert type(left) is StructureSnapshot and type(right) is StructureSnapshot
        assert all(snapshot.required and snapshot.closed and snapshot.complete and not snapshot.errors for snapshot in (left, right)), (
            "structure comparison requires closed complete observations"
        )

        def normalized(snapshot):
            clocks = {event.ordinal: index for index, event in enumerate(row for row in snapshot.events if row.kind == "clock_attempt")}
            submissions = {
                event.ordinal: index for index, event in enumerate(row for row in snapshot.events if row.kind == "submission_attempt")
            }
            groups = {}
            event_roles = {}
            windows = {}
            connections = {}
            threads = {}

            # Loop issuance/join and a worker's serial execution are separate
            # sequences. Their explicit causal edges supply the cross-thread
            # ordering; raw numeric thread IDs cannot supply a global order.
            def group_role(event):
                if event.submission is not None:
                    side = "loop" if event.kind in ("submission_attempt", "submission_join") else "worker"
                    return ("submission", submissions[event.submission], side)
                return ("clock", clocks.get(event.clock), "loop")

            for event in snapshot.events:
                if event.kind == "window_attempt":
                    owner = group_role(event)
                    windows[event.ordinal] = (owner, sum(known[0] == owner for known in windows.values()))
            for event in snapshot.events:
                owner = group_role(event)
                sequence = groups.setdefault(owner, [])
                role = (owner, len(sequence))
                event_roles[event.ordinal] = role
                window = windows.get(event.window)
                connection_key = (window or owner, event.connection)
                thread_key = (owner, event.thread)
                if event.connection is not None and connection_key not in connections:
                    connections[connection_key] = sum(key[0] == connection_key[0] for key in connections)
                if thread_key not in threads:
                    threads[thread_key] = sum(key[0] == owner for key in threads)
                sequence.append(
                    (
                        event.kind,
                        event.phase,
                        clocks.get(event.clock),
                        event.stage,
                        event.expected_stage,
                        event.engine,
                        event.window,
                        connections.get(connection_key),
                        threads[thread_key],
                        event.source,
                        event.outcome,
                        event.result_kind,
                        event.sql,
                    )
                )
            # First pass above preserves each owner's exact ordered sequence.
            for owner, sequence in groups.items():
                groups[owner] = tuple((*row[:6], windows.get(row[6]), *row[7:]) for row in sequence)
            edges = frozenset((event_roles[edge.before], event_roles[edge.after], edge.kind) for edge in snapshot.edges)
            assert len(edges) == len(snapshot.edges), "structure comparison differs"
            # Each original public owner needs exactly one balanced terminal
            # lifetime. Only scheduling-dependent balanced resume multiplicity
            # may differ; ownership, kind and terminal outcome may not.
            expected_lifetimes = {}
            for attempt in snapshot.events:
                kind = (
                    "clock"
                    if attempt.kind == "clock_attempt"
                    else "window"
                    if attempt.kind == "window_attempt"
                    else "submission"
                    if attempt.kind == "submission_attempt"
                    else None
                )
                if kind is None:
                    continue
                terminal_kind = "submission_join" if kind == "submission" else kind + "_terminal"
                terminals = [
                    row
                    for row in snapshot.events
                    if row.kind == terminal_kind
                    and (row.submission if kind == "submission" else row.clock if kind == "clock" else row.window) == attempt.ordinal
                ]
                assert len(terminals) == 1 and terminals[0].outcome in ("returned", "unwound"), "structure comparison differs"
                key = (kind, attempt.ordinal)
                assert key not in expected_lifetimes, "structure comparison differs"
                expected_lifetimes[key] = terminals[0].outcome
            lifetimes = {}
            for life in snapshot.lifetimes:
                assert (
                    type(life) is StructureLifetime
                    and type(life.kind) is str
                    and type(life.owner) is int
                    and type(life.suspensions) is int
                    and type(life.resumptions) is int
                    and type(life.terminal) is str
                    and life.suspensions == life.resumptions
                    and life.suspensions >= 0
                ), "structure comparison differs"
                key = (life.kind, life.owner)
                assert key in expected_lifetimes and life.terminal == expected_lifetimes[key], "structure comparison differs"
                normalized_owner = event_roles[life.owner]
                key = (life.kind, normalized_owner)
                assert key not in lifetimes, "structure comparison differs"
                lifetimes[key] = life.terminal
            assert len(lifetimes) == len(expected_lifetimes), "structure comparison differs"
            return snapshot.version, snapshot.entrypoint, groups, edges, lifetimes

        assert normalized(left) == normalized(right), "structure comparison differs"

    def _ancestor(self, frame: FrameType | None, family: str | None = None) -> MemoInspection | None:
        for _ in range(64):
            if frame is None:
                return None
            record = self._frames.get(id(frame))
            if record is not None and (family is None or record.family == family):
                return record
            frame = frame.f_back
        return None

    def _catalog_schema_error(self, message: str) -> None:
        self._catalog_schema_error_count += 1
        if len(self._catalog_schema_errors) < 8:
            self._catalog_schema_errors.append(message)

    def _bind_catalog_schema(self) -> None:
        catalog = self._catalog_schema_owner
        groups = []
        bindings = []
        # Identity keys only deduplicate immutable expected facts. Values keep
        # the original owners alive; no current validation result is cached.
        slot_guards: dict[int, type] = {}
        descriptor_guards: dict[int, _CatalogSchemaDescriptorGuard] = {}
        try:
            manager = catalog.__dict__.get("_pm")
            if type(manager) is not _CATALOG_MANAGER_CLASS:
                raise TypeError("catalog schema requires its registered manager")
            self._catalog_schema_manager = manager
            for attribute, registry, kind in (
                ("_source_classes", "_sources", "source"),
                ("_transform_classes", "_transforms", "transform"),
                ("_sink_classes", "_sinks", "sink"),
            ):
                classes = catalog.__dict__[attribute]
                registered = manager.__dict__.get(registry)
                if (
                    type(classes) is not list
                    or type(registered) is not dict
                    or len(classes) != len(registered)
                    or any(a is not b for a, b in zip(classes, registered.values(), strict=True))
                ):
                    raise TypeError("catalog class group must be owned")
                groups.append((attribute, registry, kind, tuple(classes)))
                for plugin in classes:
                    if not issubclass(type(plugin), type):
                        raise TypeError("catalog plugin must be a class")
                    preceding = []
                    mro = plugin.__mro__
                    for defining in mro:
                        if "get_config_schema" not in defining.__dict__:
                            preceding.append(defining)
                            slot_guards[id(defining)] = defining
                            continue
                        anchor = next((anchor for anchor in _CATALOG_SCHEMA_ORIGINALS if anchor[0] is defining), None)
                        if anchor is None:
                            raise TypeError("catalog schema has no independent original anchor")
                        _owner, descriptor, function, code = anchor
                        bindings.append(_CatalogSchemaBinding(plugin, kind, tuple(preceding), defining, descriptor, function, code, mro))
                        if id(defining) not in descriptor_guards:
                            descriptor_guards[id(defining)] = _CatalogSchemaDescriptorGuard(defining, descriptor, function, code)
                        break
                    else:
                        raise TypeError("catalog schema method is missing")
            self._catalog_schema_groups = tuple(groups)
            self._catalog_schema_bindings = tuple(bindings)
            self._catalog_schema_slot_guards = tuple(slot_guards.values())
            self._catalog_schema_descriptor_guards = tuple(descriptor_guards.values())
            self._catalog_schema_source_valid = bool(bindings) and self._catalog_schema_sources_current()
            if not self._catalog_schema_source_valid:
                self._catalog_schema_error("catalog schema original source is unbound")
        except Exception as error:
            self._catalog_schema_error("catalog schema binding failed: " + type(error).__name__)

    def _catalog_schema_sources_current(self) -> bool:
        catalog = self._catalog_schema_owner
        if (
            type(_CATALOG_STAGE_CONTEXT) is not ContextVar
            or retention_memo._RETENTION_STAGE_MEMO is not _CATALOG_STAGE_CONTEXT
            or type(catalog) is not _CATALOG_CLASS
            or catalog_service.CatalogServiceImpl is not _CATALOG_CLASS
            or catalog_service.PluginManager is not _CATALOG_MANAGER_CLASS
            or catalog.__dict__.get("_pm") is not self._catalog_schema_manager
            or _CATALOG_CLASS.__dict__.get("_catalog_schema") is not _CATALOG_FACTORY
            or _CATALOG_FACTORY.__code__ is not _CATALOG_FACTORY_CODE
            or _CATALOG_FACTORY.__globals__ is not catalog_service.__dict__
            or not self._catalog_schema_bindings
            or any(name in catalog.__dict__ for name in ("_catalog_schema", "get_schema", "list_sources", "list_transforms", "list_sinks"))
        ):
            return False
        for function, code in zip(_CATALOG_READS, _CATALOG_READ_CODES, strict=True):
            if _CATALOG_CLASS.__dict__.get(function.__name__) is not function or function.__code__ is not code:
                return False
        for attribute, registry, _kind, classes in self._catalog_schema_groups:
            current = catalog.__dict__.get(attribute)
            if type(current) is not list or len(current) != len(classes) or any(a is not b for a, b in zip(current, classes, strict=True)):
                return False
            registered = self._catalog_schema_manager.__dict__.get(registry)
            if (
                type(registered) is not dict
                or len(registered) != len(classes)
                or any(a is not b for a, b in zip(registered.values(), classes, strict=True))
            ):
                return False
        for binding in self._catalog_schema_bindings:
            # Full original tuple identity is conservatively stronger than
            # the old prefix check, including changes below the defining owner.
            if binding.plugin.__mro__ is not binding.mro:
                return False
        for parent in self._catalog_schema_slot_guards:
            if "get_config_schema" in parent.__dict__:
                return False
        for guard in self._catalog_schema_descriptor_guards:
            if (
                guard.defining.__dict__.get("get_config_schema") is not guard.descriptor
                or guard.descriptor.__func__ is not guard.function
                or guard.function.__code__ is not guard.code
            ):
                return False
        return True

    def _catalog_schema_event(self, frame: FrameType, event: str, expected_id: int | None) -> None:
        if event == "return":
            self._catalog_schema_frames.pop(id(frame), None)
            return
        if event not in ("call", "c_call"):
            return
        # Profile events also occur while the application owns memo._lock.
        # Its public getter calls active and would reacquire that same lock.
        # Read only the original ContextVar; source/current-carrier/closed
        # checks below still govern attribution and never manufacture activity.
        actual = _CATALOG_STAGE_CONTEXT.get()
        memo = next((memo for memo in self.memos if actual is memo), None)
        stack = self._catalog_schema_scopes.get(get_ident(), ())
        if memo is None and expected_id is None and not stack:
            return
        code = frame.f_code
        category = None
        if event == "call":
            if code is _CATALOG_FACTORY_CODE:
                category = "factory"
            elif id(code) in self._catalog_schema_codes and self._catalog_schema_codes[id(code)] is code:
                category = "plugin"
            elif any(code is original for original in _CATALOG_READ_CODES):
                category = "read"
        # Only callbacks acquire this lock. Scope bookkeeping never acquires
        # it, avoiding recursive profile events while an application lock is held.
        with self._catalog_schema_lock:
            if category is not None:
                self._catalog_schema_frames[id(frame)] = (get_ident(), category)
                if memo is None:
                    self._catalog_schema_unassigned[category] += 1
                else:
                    self._catalog_schema_raw.setdefault(memo, Counter())[category] += 1
            if not self._catalog_schema_sources_current():
                self._catalog_schema_source_valid = False
                self._catalog_schema_error("catalog schema original source changed during observation")
            if memo is not None and type(memo._closed) is not bool:
                self._catalog_schema_source_valid = False
                self._catalog_schema_error("catalog schema carrier has no exact closed state")
            if memo is not None and memo._closed is False:
                self._catalog_schema_checked.add(memo)
            if category is None:
                return
            if (
                memo is None
                or memo._closed is not False
                or get_ident() not in self._byte_threads
                or self._byte_threads[get_ident()] != expected_id
                or (expected_id is not None and expected_id != id(memo))
                or (stack and stack[-1] != id(memo))
            ):
                self._catalog_schema_error("catalog schema work lost its actual stage/thread/submission carrier")
                return
            if category == "plugin":
                valid = any(
                    binding.code is code and binding.plugin is frame.f_locals.get("cls") and binding.function.__globals__ is frame.f_globals
                    for binding in self._catalog_schema_bindings
                )
            elif category == "factory":
                valid = (
                    frame.f_locals.get("self") is self._catalog_schema_owner
                    and frame.f_globals is catalog_service.__dict__
                    and any(
                        binding.plugin is frame.f_locals.get("plugin_cls") and binding.kind == frame.f_locals.get("plugin_type")
                        for binding in self._catalog_schema_bindings
                    )
                )
            else:
                valid = frame.f_locals.get("self") is self._catalog_schema_owner and frame.f_globals is catalog_service.__dict__
            if not valid or not self._catalog_schema_source_valid:
                self._catalog_schema_error("catalog schema work lacks its original bound owner/class")
                return
            self._catalog_schema_attributed.setdefault(memo, Counter())[category] += 1

    def catalog_schema_totals(self) -> tuple[CatalogSchemaTotals, ...]:
        if not self.require_catalog_schema:
            return ()
        return tuple(
            CatalogSchemaTotals(
                stage, *(raw[name] for name in ("factory", "plugin", "read")), *(attributed[name] for name in ("factory", "plugin", "read"))
            )
            for stage, memo in enumerate(self.memos, 1)
            for raw in (self._catalog_schema_raw.get(memo, Counter()),)
            for attributed in (self._catalog_schema_attributed.get(memo, Counter()),)
        )

    def catalog_schema_checked_stages(self) -> tuple[int, ...]:
        return tuple(stage for stage, memo in enumerate(self.memos, 1) if memo in self._catalog_schema_checked)

    def catalog_schema_unassigned(self) -> dict[str, int]:
        return {name: self._catalog_schema_unassigned[name] for name in ("factory", "plugin", "read")}

    def catalog_schema_closed(self) -> bool:
        return (
            self._closed
            and not self._installed
            and sys.getprofile() is self._previous
            and self._owned_hooks.issubset({self._main_hook})
            and all(not memo.active for memo in self.memos)
            and not self._catalog_schema_frames
            and not self._catalog_schema_scopes
        )

    def catalog_schema_complete(self) -> bool:
        return (
            self.require_catalog_schema
            and self.catalog_schema_closed()
            and self._catalog_schema_source_valid
            and bool(self._catalog_schema_bindings)
            and self._catalog_schema_error_count == 0
            and bool(self.memos)
            and self.catalog_schema_checked_stages() == tuple(range(1, len(self.memos) + 1))
            and not any(self.catalog_schema_unassigned().values())
            and all(
                row.factory_attempts == row.attributed_factory_attempts
                and row.plugin_attempts == row.attributed_plugin_attempts
                and row.read_attempts == row.attributed_read_attempts
                for row in self.catalog_schema_totals()
            )
        )

    def _assert_catalog_schema_budget(self, requested: bool) -> None:
        assert self.catalog_schema_closed(), "catalog schema observation remains active"
        assert requested, "catalog schema budget assertion is required"
        assert self._catalog_schema_source_valid and self._catalog_schema_error_count == 0, "catalog schema provenance invalid: " + str(
            self._catalog_schema_errors
        )
        assert self.catalog_schema_complete(), "catalog schema coverage is incomplete"
        assert all(row.factory_attempts == row.plugin_attempts == 0 for row in self.catalog_schema_totals()), (
            "catalog schema construction budget exceeded"
        )
        self._catalog_schema_verified = True

    def _catalog_schema_state(self) -> tuple[object, ...]:
        return (
            self.catalog_schema_totals(),
            self.catalog_schema_checked_stages(),
            tuple(self.catalog_schema_unassigned().values()),
            self._catalog_schema_error_count,
            tuple(self._catalog_schema_errors),
            self._catalog_schema_source_valid,
            self._catalog_schema_verified,
            len(self._catalog_schema_bindings),
        )

    def _artifact_carrier(self, expected_id: int | None, family: ArtifactFamily, errors: list[str]) -> RetentionStageMemo | None:
        actual = current_retention_stage_memo()
        if (
            type(actual) is not RetentionStageMemo
            or not any(actual is memo for memo in self.memos)
            or not actual.active
            or get_ident() not in self._byte_threads
            or self._byte_threads[get_ident()] != expected_id
            or (expected_id is not None and expected_id != id(actual))
        ):
            errors.append(f"{family} artifact lost its actual stage/thread/submission carrier")
            return None
        return actual

    def _snapshot_artifact_carrier(self, expected_id: int | None) -> RetentionStageMemo | None:
        return self._artifact_carrier(expected_id, "snapshot", self.snapshot_artifact_errors)

    @staticmethod
    def _snapshot_artifact_source_is_original() -> bool:
        return (
            retention_memo._snapshot_value is _SNAPSHOT_VALUE
            and _SNAPSHOT_VALUE.__code__ is _SNAPSHOT_VALUE_CODE
            and RetentionStageMemo.snapshot is _SNAPSHOT_QUERY
            and _SNAPSHOT_QUERY.__code__ is _SNAPSHOT_QUERY_CODE
            and continuation_codec._decode_continuation_snapshot_uncached is _SNAPSHOT_PRODUCER
            and _SNAPSHOT_PRODUCER.__code__ is _SNAPSHOT_WORK_CODE
            and continuation_codec.parse_json_strict is _STRICT_PARSE
            and json_parser.parse_json_strict is _STRICT_PARSE
            and _STRICT_PARSE.__code__ is _PARSE
            and continuation_codec.canonical_json is _ORIGINAL_CANONICAL
            and hashing.canonical_json is _ORIGINAL_CANONICAL
            and _ORIGINAL_CANONICAL.__code__ is _CANONICAL
            and continuation_policy.decode_continuation_policy is _SNAPSHOT_POLICY
            and _SNAPSHOT_POLICY.__code__ is _SNAPSHOT_POLICY_CODE
            and hashing.rfc8785.dumps is _RFC_DUMPS
            and (type(_RFC_DUMPS) is not FunctionType or _RFC_DUMPS.__code__ is _SNAPSHOT_RFC_CODE)
        )

    @staticmethod
    def _chunk_artifact_source_is_original() -> bool:
        return (
            continuation_codec.verify_private_chunk is _CHUNK_PUBLIC
            and _CHUNK_PUBLIC.__code__ is _CHUNK_REQUEST
            and RetentionStageMemo.private_chunk is _CHUNK_QUERY
            and _CHUNK_QUERY.__code__ is _CHUNK_QUERY_CODE
            and retention_memo._chunk_value is _CHUNK_VALUE
            and _CHUNK_VALUE.__code__ is _CHUNK_VALUE_CODE
            and continuation_codec._verify_private_chunk_canonical is _CHUNK_PRODUCER
            and _CHUNK_PRODUCER.__code__ is _CHUNK_WORK_CODE
            and continuation_codec._private_chunk_memo_reference is _CHUNK_REFERENCE
            and _CHUNK_REFERENCE.__code__ is _CHUNK_REFERENCE_CODE
            and continuation_codec.PrivateChunkVerificationKey is PrivateChunkVerificationKey
            and continuation_codec.PrivateChunkRef is PrivateChunkRef
            and continuation_codec.parse_json_strict is _STRICT_PARSE
            and json_parser.parse_json_strict is _STRICT_PARSE
            and _STRICT_PARSE.__code__ is _PARSE
            and continuation_codec.canonical_json is _ORIGINAL_CANONICAL
            and hashing.canonical_json is _ORIGINAL_CANONICAL
            and _ORIGINAL_CANONICAL.__code__ is _CANONICAL
            and hashing.rfc8785.dumps is _RFC_DUMPS
            and (type(_RFC_DUMPS) is not FunctionType or _RFC_DUMPS.__code__ is _SNAPSHOT_RFC_CODE)
            and continuation_codec.sha256 is _SHA256
        )

    @staticmethod
    def _artifact_text(key: object, family: ArtifactFamily) -> str | None:
        if family == "snapshot":
            return key.encoded if type(key) is SnapshotDecodeKey and type(key.encoded) is str else None
        assert family == "chunk"
        return key.canonical if type(key) is PrivateChunkVerificationKey and _safe_key(key) else None

    @staticmethod
    def _artifact_record(record: MemoInspection, family: ArtifactFamily) -> tuple[bool, int]:
        if family == "snapshot":
            return record.snapshot_artifact_bound, record.snapshot_artifact_attempts
        assert family == "chunk"
        return record.chunk_artifact_bound, record.chunk_artifact_attempts

    def _artifact_input_matches(self, key: object, high: FrameType, family: ArtifactFamily) -> bool:
        text = self._artifact_text(key, family)
        if text is None:
            return False
        if family == "snapshot":
            return text is high.f_locals.get("encoded") and continuation_codec.SnapshotDecodeKey is SnapshotDecodeKey
        return text is high.f_locals.get("canonical") and key.reference is high.f_locals.get("reference")

    def _snapshot_artifact_event(self, frame: FrameType, event: str, expected_id: int | None) -> None:
        self._artifact_event(
            frame,
            event,
            expected_id,
            "snapshot",
            self._snapshot_artifact_requests,
            self._snapshot_artifact_observed,
            self.snapshot_artifact_errors,
        )

    def _chunk_artifact_event(self, frame: FrameType, event: str, expected_id: int | None) -> None:
        self._artifact_event(
            frame,
            event,
            expected_id,
            "chunk",
            self._chunk_artifact_requests,
            self._chunk_artifact_observed,
            self.chunk_artifact_errors,
        )

    def _artifact_event(
        self,
        frame: FrameType,
        event: str,
        expected_id: int | None,
        family: ArtifactFamily,
        requests: dict[int, _ArtifactRequest],
        observed: Counter[RetentionStageMemo],
        errors: list[str],
    ) -> None:
        if family == "snapshot":
            high_code, query_code, work_code, value_code = (
                _SNAPSHOT_REQUEST,
                _SNAPSHOT_QUERY_CODE,
                _SNAPSHOT_WORK_CODE,
                _SNAPSHOT_VALUE_CODE,
            )
            query_site, work_site, producer = _SNAPSHOT_QUERY_SITE, _SNAPSHOT_WORK_SITE, _SNAPSHOT_PRODUCER
            noun = "decoder"
        else:
            assert family == "chunk"
            high_code, query_code, work_code, value_code = _CHUNK_REQUEST, _CHUNK_QUERY_CODE, _CHUNK_WORK_CODE, _CHUNK_VALUE_CODE
            query_site, work_site, producer = _CHUNK_QUERY_SITE, _CHUNK_WORK_SITE, _CHUNK_PRODUCER
            noun = "verifier"
        code = frame.f_code
        if code is high_code and event == "return":
            state = requests.pop(id(frame), None)
            if state is not None and state.key_id is None:
                errors.append(f"{family} artifact request lacks its original complete key")
            return
        if event != "call" or not any(code is target for target in (high_code, query_code, work_code)):
            return
        actual = current_retention_stage_memo()
        if actual is None and expected_id is None:
            if any(state.thread_id == get_ident() for state in requests.values()):
                errors.append(f"{family} artifact lost its actual stage/thread/submission carrier")
            return
        if code is work_code and type(actual) is RetentionStageMemo and any(actual is memo for memo in self.memos):
            with self._lock:
                observed[actual] += 1
        memo = self._artifact_carrier(expected_id, family, errors)
        if memo is None:
            return
        with self._lock:
            if code is high_code:
                record = self._frames.get(id(frame))
                if frame.f_globals is not continuation_codec.__dict__ or record is None or record.family != family:
                    errors.append(f"{family} artifact request lacks its original source")
                    return
                ordinal = next(index for index, candidate in enumerate(self.records, 1) if candidate is record)
                requests[id(frame)] = _ArtifactRequest(ordinal, id(memo), get_ident(), expected_id)
                return
            source_original = (
                self._snapshot_artifact_source_is_original() if family == "snapshot" else self._chunk_artifact_source_is_original()
            )
            if code is query_code:
                parent = frame.f_back
                state = None if parent is None else requests.get(id(parent))
                if (
                    parent is None
                    or state is None
                    or not self._at_site(parent, high_code, query_site)
                    or frame.f_globals is not retention_memo.__dict__
                    or not source_original
                ):
                    errors.append(f"{family} artifact query lost its original direct callsite")
                    return
                record = self.records[state.record_ordinal - 1]
                key = frame.f_locals.get("key")
                if (
                    not self._artifact_input_matches(key, parent, family)
                    or record.key is not key
                    or frame.f_locals.get("self") is not memo
                    or record.memo is not memo
                    or frame.f_locals.get("producer") is not producer
                    or record.producer is not producer
                    or state.memo_id != id(memo)
                    or state.thread_id != get_ident()
                    or state.expected_memo_id != expected_id
                ):
                    errors.append(f"{family} artifact query lost its exact key/producer/carrier")
                    return
                state.key_id = id(key)
                if family == "snapshot":
                    record.snapshot_artifact_bound = True
                else:
                    record.chunk_artifact_bound = True
                return
            parent = frame.f_back
            if (
                parent is None
                or not self._at_site(parent, value_code, work_site)
                or parent.f_globals is not retention_memo.__dict__
                or frame.f_globals is not continuation_codec.__dict__
                or not source_original
            ):
                errors.append(f"{family} artifact {noun} lost its original body/callsite/globals")
                return
            ancestor = parent.f_back
            for _ in range(64):
                if ancestor is None or ancestor.f_code is query_code:
                    break
                ancestor = ancestor.f_back
            else:
                ancestor = None
            high = None if ancestor is None else ancestor.f_back
            state = None if high is None else requests.get(id(high))
            if (
                ancestor is None
                or high is None
                or state is None
                or not self._at_site(high, high_code, query_site)
                or ancestor.f_globals is not retention_memo.__dict__
            ):
                errors.append(f"{family} artifact {noun} lacks its original query owner")
                return
            record = self.records[state.record_ordinal - 1]
            key = record.key
            text = self._artifact_text(key, family)
            if (
                text is None
                or id(key) != state.key_id
                or not self._artifact_record(record, family)[0]
                or record.memo is not memo
                or record.producer is not producer
                or parent.f_locals.get("key") is not key
                or parent.f_locals.get("producer") is not producer
                or ancestor.f_locals.get("key") is not key
                or ancestor.f_locals.get("self") is not memo
                or frame.f_locals.get("encoded" if family == "snapshot" else "canonical") is not text
                or not self._artifact_input_matches(key, high, family)
                or state.memo_id != id(memo)
                or state.thread_id != get_ident()
                or state.expected_memo_id != expected_id
            ):
                errors.append(f"{family} artifact {noun} lost its complete original key/input/producer")
                return
            if family == "snapshot":
                record.snapshot_artifact_attempts += 1
            else:
                record.chunk_artifact_attempts += 1

    def _artifact_closed(self, requests: dict[int, _ArtifactRequest]) -> bool:
        return (
            self._closed
            and not self._installed
            and sys.getprofile() is self._previous
            and self._owned_hooks.issubset({self._main_hook})
            and all(type(memo) is RetentionStageMemo and not memo.active for memo in self.memos)
            and (self._byte_monitor is None or self._byte_monitor.closed)
            and not requests
        )

    def snapshot_artifact_closed(self) -> bool:
        return self._artifact_closed(self._snapshot_artifact_requests)

    def chunk_artifact_closed(self) -> bool:
        return self._artifact_closed(self._chunk_artifact_requests)

    def _measure_artifacts(self, family: ArtifactFamily, observed: Counter[RetentionStageMemo], errors: list[str]):
        if family == "snapshot":
            producer, row_type, stage_type, size_source = (
                _SNAPSHOT_PRODUCER,
                SnapshotArtifactTotals,
                SnapshotArtifactStageTotals,
                _snapshot_source_utf8_extent,
            )
            producer_name = "_decode_continuation_snapshot_uncached"
            basis = _SNAPSHOT_ARTIFACT_BASIS
        else:
            assert family == "chunk"
            producer, row_type, stage_type, size_source = (
                _CHUNK_PRODUCER,
                ChunkArtifactTotals,
                ChunkArtifactStageTotals,
                _chunk_source_utf8_extent,
            )
            producer_name = "_verify_private_chunk_canonical"
            basis = _CHUNK_ARTIFACT_BASIS
        rows, sizing_failed = [], False
        for stage, memo in enumerate(self.memos, 1):
            records = [record for record in self.records if record.memo is memo and record.family == family]
            groups = []
            for record in records:
                if (
                    not self._artifact_record(record, family)[0]
                    or self._artifact_text(record.key, family) is None
                    or record.producer is not producer
                ):
                    errors.append(f"{family} artifact source key was not completely observed")
                    continue
                if record.group not in groups:
                    groups.append(record.group)
            for group in groups:
                calls = [record for record in records if self._artifact_record(record, family)[0] and record.group == group]
                attempts = sum(self._artifact_record(record, family)[1] for record in calls)
                extent = None
                try:
                    source = self._artifact_text(calls[0].key, family)
                    if source is None:
                        raise TypeError("source key lost its complete text")
                    extent = size_source(source)
                    if type(extent) is not int or extent < 0:
                        raise TypeError("source extent must be a nonnegative integer")
                except Exception as error:
                    extent, sizing_failed = None, True
                    errors.append(f"{family} artifact source UTF8 sizing failed: " + type(error).__name__)
                rows.append(
                    row_type(
                        stage,
                        len(rows) + 1,
                        producer_name,
                        basis,
                        len(calls),
                        attempts,
                        extent,
                        None if extent is None else attempts * extent,
                    )
                )
        stages = tuple(
            stage_type(
                stage,
                basis,
                len(stage_rows),
                sum(record.memo is memo and record.family == family for record in self.records),
                sum(row.work_attempts for row in stage_rows),
                observed[memo],
                None
                if any(row.processed_source_utf8_extent is None for row in stage_rows)
                else sum(row.processed_source_utf8_extent for row in stage_rows),
                None if any(row.source_utf8_extent is None for row in stage_rows) else sum(row.source_utf8_extent for row in stage_rows),
            )
            for stage, memo in enumerate(self.memos, 1)
            for stage_rows in ([row for row in rows if row.stage == stage],)
        )
        return tuple(rows), stages, sizing_failed

    def _size_snapshot_artifacts(self) -> None:
        if self._snapshot_artifact_sized:
            return
        if not self.snapshot_artifact_closed():
            self.snapshot_artifact_errors.append("snapshot artifact sizing requires closed observation")
            return
        self._snapshot_artifact_rows, self._snapshot_artifact_stages, self._snapshot_artifact_sizing_failed = self._measure_artifacts(
            "snapshot",
            self._snapshot_artifact_observed,
            self.snapshot_artifact_errors,
        )
        self._snapshot_artifact_sized = True

    def _size_chunk_artifacts(self) -> None:
        if self._chunk_artifact_sized:
            return
        if not self.chunk_artifact_closed():
            self.chunk_artifact_errors.append("chunk artifact sizing requires closed observation")
            return
        self._chunk_artifact_rows, self._chunk_artifact_stages, self._chunk_artifact_sizing_failed = self._measure_artifacts(
            "chunk",
            self._chunk_artifact_observed,
            self.chunk_artifact_errors,
        )
        self._chunk_artifact_sized = True

    def snapshot_artifact_totals(self) -> tuple[SnapshotArtifactTotals, ...]:
        return self._snapshot_artifact_rows

    def snapshot_artifact_stage_totals(self) -> tuple[SnapshotArtifactStageTotals, ...]:
        return self._snapshot_artifact_stages

    def chunk_artifact_totals(self) -> tuple[ChunkArtifactTotals, ...]:
        return self._chunk_artifact_rows

    def chunk_artifact_stage_totals(self) -> tuple[ChunkArtifactStageTotals, ...]:
        return self._chunk_artifact_stages

    @staticmethod
    def _artifact_complete(closed: bool, errors, rows, stages) -> bool:
        return (
            closed
            and not errors
            and bool(rows)
            and all(row.source_utf8_extent is not None for row in rows)
            and all(
                total.work_attempts == total.observed_work_attempts
                and total.requests == sum(row.requests for row in rows if row.stage == total.stage)
                for total in stages
            )
        )

    def snapshot_artifact_complete(self) -> bool:
        return (
            self.require_snapshot_artifacts
            and self._snapshot_artifact_sized
            and self._artifact_complete(
                self.snapshot_artifact_closed(),
                self.snapshot_artifact_errors,
                self._snapshot_artifact_rows,
                self._snapshot_artifact_stages,
            )
        )

    def chunk_artifact_complete(self) -> bool:
        return (
            self.require_chunk_artifacts
            and self._chunk_artifact_sized
            and self._artifact_complete(
                self.chunk_artifact_closed(),
                self.chunk_artifact_errors,
                self._chunk_artifact_rows,
                self._chunk_artifact_stages,
            )
        )

    @staticmethod
    def _assert_artifact_work(family: ArtifactFamily, requested: bool, closed: bool, sized: bool, errors, rows) -> None:
        assert closed, f"{family} artifact metric requires closed observation"
        assert requested, f"{family} artifact budget assertion is required"
        assert not errors, f"{family} artifact provenance invalid: " + str(errors)
        assert sized, f"{family} artifact metric is missing"
        assert rows and all(row.source_utf8_extent is not None and row.source_utf8_extent > 0 for row in rows), (
            f"{family} artifact inputs must be nonempty"
        )
        assert all(row.work_attempts >= 1 for row in rows), f"{family} artifact actual work is missing"
        assert all(row.work_attempts <= 1 for row in rows), f"{family} artifact work budget exceeded"

    def _assert_snapshot_artifact_budget(self, requested: bool) -> None:
        self._assert_artifact_work(
            "snapshot",
            requested,
            self.snapshot_artifact_closed(),
            self._snapshot_artifact_sized,
            self.snapshot_artifact_errors,
            self.snapshot_artifact_totals(),
        )
        self._assert_snapshot_artifact_byte_ratio()
        self._snapshot_artifact_verified = True

    def _assert_chunk_artifact_budget(self, requested: bool) -> None:
        self._assert_artifact_work(
            "chunk",
            requested,
            self.chunk_artifact_closed(),
            self._chunk_artifact_sized,
            self.chunk_artifact_errors,
            self.chunk_artifact_totals(),
        )
        self._assert_chunk_artifact_byte_ratio()
        self._chunk_artifact_verified = True

    @staticmethod
    def _assert_artifact_byte_ratio(family: ArtifactFamily, closed: bool, sized: bool, rows, stages) -> None:
        assert closed and sized, f"{family} artifact metric is missing"
        for total in stages:
            assert total.denominator is not None and total.denominator > 0, f"{family} artifact stage inputs must be nonempty"
            assert total.work_attempts == total.observed_work_attempts, f"{family} artifact actual work lost its source attribution"
            assert total.requests == sum(row.requests for row in rows if row.stage == total.stage), (
                f"{family} artifact requests lost their keys"
            )
            assert total.numerator is not None and total.denominator is not None and total.numerator <= total.denominator, (
                f"{family} artifact weighted source extent budget exceeded"
            )

    def _assert_snapshot_artifact_byte_ratio(self) -> None:
        self._assert_artifact_byte_ratio(
            "snapshot",
            self.snapshot_artifact_closed(),
            self._snapshot_artifact_sized,
            self.snapshot_artifact_totals(),
            self.snapshot_artifact_stage_totals(),
        )

    def _assert_chunk_artifact_byte_ratio(self) -> None:
        self._assert_artifact_byte_ratio(
            "chunk",
            self.chunk_artifact_closed(),
            self._chunk_artifact_sized,
            self.chunk_artifact_totals(),
            self.chunk_artifact_stage_totals(),
        )

    def _byte_error(self, message: str, frame: FrameType, expected_id: int | None, owner=None) -> None:
        """Keep original errors and a finite source-only sample, never DATA."""
        with self._byte_diagnostic_lock:
            self.byte_errors.append(message)
            if len(self.byte_error_diagnostics) >= 8:
                return
            sites = []
            shortened = False
            ancestor: FrameType | None = frame
            for _ in range(64):
                if ancestor is None:
                    break
                code = ancestor.f_code
                shortened |= len(code.co_filename) > 512 or len(code.co_qualname) > 160
                sites.append(MemoCallSite(code.co_filename[:512], code.co_qualname[:160], code.co_firstlineno, ancestor.f_lineno))
                ancestor = ancestor.f_back
            actual = current_retention_stage_memo()
            stage = next((index for index, memo in enumerate(self.memos, 1) if memo is actual), None)
            signature = (message, stage, *sites[:4])
            if signature in self._byte_diagnostic_sites:
                return
            self._byte_diagnostic_sites.add(signature)
            kind, ordinal, family = None, None, None
            if type(owner) is MemoInspection:
                kind, family = "memo_request", owner.family
                ordinal = next(index for index, record in enumerate(self.records, 1) if record is owner)
            elif type(owner) is UncachedValidationInvocation:
                kind, ordinal, family = "uncached_validation", owner.invocation, owner.validator
            elif type(owner) is ContextCanonicalInspection:
                kind, ordinal, family = "context_capture", owner.invocation, _CONTEXT_CANONICAL_NAME
            self.byte_error_diagnostics.append(
                NativeByteErrorDiagnostic(
                    len(self.byte_errors),
                    message,
                    stage,
                    None if actual is None else id(actual),
                    actual.active if type(actual) is RetentionStageMemo else None,
                    get_ident(),
                    expected_id,
                    kind,
                    ordinal,
                    family,
                    tuple(sites),
                    shortened or ancestor is not None,
                )
            )

    def _defer_chunk_parse(self, frame: FrameType, record: MemoInspection, expected_id: int | None) -> bool:
        caller = frame.f_back
        text = frame.f_locals.get("text")
        key = record.key
        if (
            caller is None
            or not self._at_site(caller, _LOW_CODE_OBJECTS[1][0], _CHUNK_PARSE_SITE)
            or caller.f_globals is not continuation_codec.__dict__
            or frame.f_globals is not json_parser.__dict__
            or continuation_codec.parse_json_strict is not _STRICT_PARSE
            or continuation_codec._verify_private_chunk_canonical is not _CHUNK_PRODUCER
            or record.family != "chunk"
            or type(key) is not PrivateChunkVerificationKey
            or not _safe_key(key)
            or record.producer is not _CHUNK_PRODUCER
            or type(text) is not str
            or text is not key.canonical
        ):
            return False
        ancestor = caller.f_back
        for _ in range(64):
            if ancestor is None:
                return False
            if ancestor.f_code is _CHUNK_REQUEST and self._frames.get(id(ancestor)) is record:
                if (
                    ancestor.f_globals is not continuation_codec.__dict__
                    or ancestor.f_locals.get("canonical") is not text
                    or ancestor.f_locals.get("reference") is not key.reference
                ):
                    return False
                ordinal = next(index for index, candidate in enumerate(self.records, 1) if candidate is record)
                self._byte_pending_parses.setdefault(id(ancestor), []).append(
                    _PendingChunkParse(
                        ordinal,
                        id(record.memo),
                        id(text),
                        get_ident(),
                        expected_id,
                    )
                )
                return True
            ancestor = ancestor.f_back
        return False

    def _bind_pending_chunk(self, frame: FrameType, owner: MemoInspection, operand: bytes, expected_id: int | None) -> None:
        for pending in self._byte_pending_parses.pop(id(frame), ()):
            key = owner.key
            if (
                self.records[pending.record_ordinal - 1] is not owner
                or pending.memo_id != id(owner.memo)
                or pending.thread_id != get_ident()
                or pending.expected_memo_id != expected_id
                or type(key) is not PrivateChunkVerificationKey
                or not _safe_key(key)
                or owner.producer is not _CHUNK_PRODUCER
                or id(key.canonical) != pending.text_id
                or frame.f_globals is not continuation_codec.__dict__
                or continuation_codec.parse_json_strict is not _STRICT_PARSE
                or continuation_codec._verify_private_chunk_canonical is not _CHUNK_PRODUCER
                or frame.f_locals.get("canonical") is not key.canonical
                or frame.f_locals.get("reference") is not key.reference
            ):
                self._byte_error("strict parser has no bound native byte provenance", frame, expected_id, owner)
                continue
            owner.byte_calls["strict_parse"] += 1
            owner.byte_counts["strict_parse"] += len(operand)

    @staticmethod
    def _context_boundary(frame: FrameType | None) -> tuple[FrameType | None, bool]:
        """Return a boundary plus explicit uncertainty at the fixed bound."""
        for _ in range(64):
            if frame is None or frame.f_code is _CONTEXT_VALIDATOR:
                return frame, False
            frame = frame.f_back
        return None, frame is not None

    def _canonical_byte_owner(self, frame: FrameType) -> MemoInspection | ContextCanonicalInspection | None:
        boundary, exhausted = self._context_boundary(frame.f_back)
        if exhausted:
            self._byte_error(
                "capture ancestry search exhausted; canonical ownership is unproven", frame, self._byte_threads.get(get_ident())
            )
            return None
        if boundary is None:
            return self._ancestor(frame.f_back)
        owner = self._context_byte_frames.get(id(boundary))
        if owner is None:
            return None
        # A capture boundary never lends canonical work to an outer memo or
        # its last scalar. Only its direct original complete-context call owns
        # this buffer; wrappers and unrelated objects remain unsupported.
        if (
            frame.f_back is not boundary
            or not self._original_context_canonical(frame)
            or owner.exited
            or owner.thread_id != get_ident()
            or boundary.f_globals is not provider_context.__dict__
            or frame.f_globals is not hashing.__dict__
            or provider_context.canonical_json is not _CONTEXT_CANONICAL
            or provider_context._check_messages is not _CONTEXT_CHECK_MESSAGES
            or provider_context._check_tools is not _CONTEXT_CHECK_TOOLS
            or provider_context._copy_json is not _CONTEXT_COPY_JSON
            or provider_context._POLICY is not _CONTEXT_POLICY
            or _policy_data(provider_context._POLICY) != _CONTEXT_POLICY_KEY
        ):
            self._byte_error(
                "provider context canonical work lost its complete capture owner", frame, self._byte_threads.get(get_ident()), owner
            )
            return None
        return owner

    def _original_context_canonical(self, frame: FrameType) -> bool:
        parent = frame.f_back
        return (
            parent is not None
            and self._at_site(parent, _CONTEXT_VALIDATOR, _CONTEXT_CANONICAL_SITE)
            and type(parent.f_locals.get("context")) is dict
            and frame.f_locals.get("obj") is parent.f_locals.get("context")
        )

    def _byte_carrier(self, expected_id: int | None, frame: FrameType | None = None) -> RetentionStageMemo | None:
        source = sys._getframe(1) if frame is None else frame
        actual = current_retention_stage_memo()
        if actual is None or not any(actual is memo for memo in self.memos):
            self._byte_error("native bytes lack their observed stage carrier", source, expected_id)
            return None
        if (
            not actual.active
            or get_ident() not in self._byte_threads
            or self._byte_threads[get_ident()] != expected_id
            or (expected_id is not None and expected_id != id(actual))
        ):
            self._byte_error("native bytes disagree with their thread or submission carrier", source, expected_id)
            return None
        return actual

    @staticmethod
    def _at_site(frame: FrameType, code: CodeType, site: tuple[range, int]) -> bool:
        return frame.f_code is code and frame.f_lasti in site[0] and frame.f_lineno == site[1]

    def _parse_bytes(self, frame: FrameType) -> int | None:
        """Borrow bytes only while the original validating caller is active."""
        text = frame.f_locals.get("text")
        caller = frame.f_back
        if type(text) is not str or caller is None:
            return None
        if self._at_site(caller, _ENCODE_CHUNK, _ENCODER_PARSE_SITE):
            encoded = caller.f_locals.get("encoded")
            if caller.f_locals.get("canonical") is text and type(encoded) is bytes:
                return len(encoded)
            return None
        if self._at_site(caller, _LOW_CODE_OBJECTS[0][0], _SNAPSHOT_PARSE_SITE):
            source_code, source_site, variable = _LOAD_SNAPSHOT, _SNAPSHOT_SOURCE_SITE, "encoded"
        elif self._at_site(caller, _LOW_CODE_OBJECTS[1][0], _CHUNK_PARSE_SITE):
            source_code, source_site, variable = _LOAD_CHUNKS, _CHUNK_SOURCE_SITE, "canonical"
        else:
            # Nested policy/action text has no native UTF-8 buffer at these
            # sites. A canonical output is not its parser-input provenance.
            return None
        ancestor = caller.f_back
        for _ in range(64):
            if ancestor is None:
                return None
            if self._at_site(ancestor, source_code, source_site):
                if ancestor.f_locals.get(variable) is not text:
                    return None
                if source_code is _LOAD_SNAPSHOT:
                    if stored_continuation.read_chunk is not _READ_CHUNK or stored_continuation.sha256 is not _SHA256:
                        return None
                    payload = ancestor.f_locals.get("payload")
                    native = self._byte_sources.get(id(ancestor))
                    if type(payload) is not bytes or native is None or id(payload) != native[0]:
                        return None
                else:
                    if (
                        continuation_selection.read_private_chunk is not _READ_PRIVATE_CHUNK
                        or stored_continuation.read_private_chunk is not _READ_PRIVATE_CHUNK
                        or _READ_PRIVATE_CHUNK.__code__ is not _READ_PRIVATE_CHUNK_CODE
                        or stored_continuation.sha256 is not _SHA256
                    ):
                        return None
                    native = self._byte_sources.get(id(ancestor))
                # The exact original source site assigned this text from the
                # previously observed native bytes.decode receiver. Never read
                # the row again or retain its payload in an observer record.
                return None if native is None else native[1]
            ancestor = ancestor.f_back
        return None

    def _scanner_size_call(self, frame: FrameType, offset: int, callable_, operand) -> None:
        expected = self._byte_threads.get(get_ident())
        actual = current_retention_stage_memo()
        ancestor = frame.f_back
        owner = None
        for _ in range(64):
            if ancestor is None:
                break
            owner = self._byte_detector_frames.get(id(ancestor))
            if owner is not None:
                break
            ancestor = ancestor.f_back
        if actual is None and owner is None and expected is None:
            return
        if self._byte_carrier(expected, frame) is None:
            return
        variable = _SCANNER_SIZE_SITES[frame.f_code][offset]
        state = frame.f_locals.get("state")
        policy = state.policy if type(state) is credential_material._TraversalState else frame.f_locals.get("policy")
        limit = policy.max_string_bytes if type(policy) is credential_material.CredentialTraversalPolicy else None
        if frame.f_code is _REFERENCE and variable == "ref" and type(limit) is int:
            limit = min(limit, _REFERENCE_MAX_BYTES)
        if (
            not _scanner_sources_current()
            or callable_ is not _UTF8_SIZE
            or frame.f_globals is not credential_material.__dict__
            or type(operand) is not str
            or operand is not frame.f_locals.get(variable)
            or type(limit) is not int
            or owner is None
            or owner.memo is not actual
            or id(frame) in self._byte_size_pending
            or (variable == "ref" and frame.f_locals.get("ref_limit") != limit)
        ):
            self._byte_error("bounded scanner changed its original source, input, limit or owner", frame, expected, owner)
            return
        self._byte_size_pending[id(frame)] = _ScannerSizeInspection(
            owner, id(frame), id(operand), str.__len__(operand), limit, get_ident(), expected, call_code=frame.f_code, call_offset=offset
        )

    def _expect_private_size(self, frame: FrameType, expected_id: int | None) -> None:
        parent = frame.f_back
        if parent is None or not self._at_site(parent, _LOAD_CHUNKS, _CHUNK_SOURCE_SITE):
            return
        actual = self._byte_carrier(expected_id, frame)
        if actual is None:
            return
        reference, canonical = frame.f_locals.get("reference"), frame.f_locals.get("canonical")
        owner = next(
            (
                record
                for record in reversed(self.records)
                if record.memo is actual
                and type(record.key) is PrivateChunkVerificationKey
                and record.key.reference is reference
                and record.key.canonical is canonical
            ),
            None,
        )
        payload = parent.f_locals.get("payload")
        previous = self._byte_private_expected.get(id(parent))
        if (
            not _private_size_sources_current()
            or owner is None
            or type(reference) is not PrivateChunkRef
            or type(canonical) is not str
            or parent.f_locals.get("ref") is not reference
            or parent.f_locals.get("canonical") is not canonical
            or type(payload) is not bytes
            or self._byte_sources.get(id(parent)) != (id(payload), len(payload))
            or owner.byte_calls["chunk_length"] != 1
            or owner.byte_calls["chunk_digest"] != 1
            or owner.byte_counts["chunk_length"] != reference.byte_length
            or owner.byte_counts["chunk_digest"] != reference.byte_length
            or (previous is not None and (previous.inspection is None or previous.inspection.branch is None))
        ):
            self._byte_error("private resource size lacks its original verified DATA request", frame, expected_id, owner)
            return
        expectation = _PrivateSizeExpectation(
            owner, id(parent), id(reference), id(canonical), str.__len__(canonical), reference.byte_length, get_ident(), expected_id
        )
        self._byte_private_requests[id(owner)] += 1
        self._byte_private_expected[id(parent)] = expectation
        self._byte_private_sizes.append(expectation)

    def _private_size_call(self, frame: FrameType, expected_id: int | None) -> bool:
        parent = frame.f_back
        loader = None if parent is None else parent.f_back
        if parent is None or loader is None:
            return False
        expectation = self._byte_private_expected.get(id(loader))
        chunk = parent.f_locals.get("chunk")
        if (
            expectation is None
            or not self._at_site(parent, _NESTED_CHUNKS, _NESTED_SIZE_SITE)
            or not self._at_site(loader, _LOAD_CHUNKS, _NESTED_LOADER_SITE)
            or not _private_size_sources_current()
            or frame.f_globals is not bounded_serialization.__dict__
            or parent.f_globals is not continuation_selection.__dict__
            or loader.f_globals is not continuation_selection.__dict__
            or type(chunk) is not continuation_codec.PrivateChunk
            or loader.f_locals.get("actual") is not chunk
            or loader.f_locals.get("ref") is not chunk.reference
            or id(chunk.reference) != expectation.reference
            or id(chunk.canonical) != expectation.text
            or frame.f_locals.get("text") is not chunk.canonical
            or frame.f_locals.get("errors") != "strict"
            or type(frame.f_locals.get("limit")) is not int
            or frame.f_locals["limit"] != _PRIVATE_UTF8_LIMIT
            or expectation.thread != get_ident()
            or expectation.expected_memo_id != expected_id
            or self._byte_carrier(expected_id, frame) is not expectation.owner.memo
        ):
            return False
        expectation.calls += 1
        if expectation.calls != 1:
            self._byte_error("private resource size duplicated its verified request", frame, expected_id, expectation.owner)
            return True
        inspection = _ScannerSizeInspection(
            expectation.owner,
            id(parent),
            expectation.text,
            expectation.characters,
            _PRIVATE_UTF8_LIMIT,
            get_ident(),
            expected_id,
            private_resource=True,
        )
        expectation.inspection = inspection
        self._byte_size_frames[id(frame)] = inspection
        return True

    def _scanner_size_profile(self, frame: FrameType, event: str, arg: object, expected_id: int | None) -> None:
        if frame.f_code in _SCANNER_SIZE_SITES:
            if event == "return" and self._byte_size_pending.pop(id(frame), None) is not None:
                self._byte_error("bounded scanner invocation was not observed", frame, expected_id)
            return
        if frame.f_code is not _UTF8_CODE or event not in ("call", "return"):
            return
        if event == "call":
            parent = frame.f_back
            inspection = self._byte_size_pending.pop(id(parent), None)
            if inspection is None:
                if self._private_size_call(frame, expected_id):
                    return
                if any(current_retention_stage_memo() is memo for memo in self.memos) or expected_id is not None:
                    self._byte_error("bounded scanner lacks its exact charged invocation", frame, expected_id)
                return
            if (
                parent is None
                or id(parent) != inspection.caller
                or inspection.call_code is None
                or type(inspection.call_offset) is not int
                or inspection.profile_offset is not None
                or parent.f_code is not inspection.call_code
                or inspection.call_code not in _SCANNER_SIZE_WINDOWS
                or inspection.call_offset not in _SCANNER_SIZE_WINDOWS[inspection.call_code]
                or not self._at_site(parent, inspection.call_code, _SCANNER_SIZE_WINDOWS[inspection.call_code][inspection.call_offset])
                or not _scanner_sources_current()
                or frame.f_globals is not bounded_serialization.__dict__
                or id(frame.f_locals.get("text")) != inspection.text
                or frame.f_locals.get("errors") != "replace"
                or type(frame.f_locals.get("limit")) is not int
                or frame.f_locals["limit"] != inspection.limit
                or inspection.thread != get_ident()
                or inspection.expected_memo_id != expected_id
                or self._byte_carrier(expected_id, frame) is not inspection.owner.memo
            ):
                self._byte_error("bounded scanner changed its original arguments or carrier", frame, expected_id, inspection.owner)
                return
            self._byte_size_frames[id(frame)] = inspection
            inspection.profile_offset = parent.f_lasti
            self._byte_size_inspections.append(inspection)
            inspection.owner.byte_calls["scanner_size"] += 1
            return
        inspection = self._byte_size_frames.pop(id(frame), None)
        if inspection is None:
            return
        branch = _UTF8_RETURN_BRANCHES.get(frame.f_lasti)
        length = frame.f_locals.get("length")
        size = frame.f_locals.get("size")
        if (
            not _scanner_sources_current()
            or self._byte_carrier(expected_id, frame) is not inspection.owner.memo
            or inspection.thread != get_ident()
            or inspection.expected_memo_id != expected_id
            or type(arg) is not int
            or length != inspection.characters
            or branch is None
        ):
            self._byte_error("bounded scanner return lost its original branch or carrier", frame, expected_id, inspection.owner)
            return
        if branch == "length_excess":
            valid = length > inspection.limit and inspection.chunks == 0 and arg == inspection.limit + 1
        elif branch == "ascii":
            valid = length <= inspection.limit and inspection.chunks == 0 and arg == length
        else:
            valid = (
                length <= inspection.limit
                and inspection.chunks > 0
                and size == inspection.encoded_bytes
                and frame.f_locals.get("start") == (inspection.chunks - 1) * _UTF8_CHUNK_CHARACTERS
                and (
                    size > inspection.limit and arg == inspection.limit + 1
                    if branch == "encoded_excess"
                    else size <= inspection.limit
                    and arg == size
                    and inspection.chunks == (length + _UTF8_CHUNK_CHARACTERS - 1) // _UTF8_CHUNK_CHARACTERS
                )
            )
        if not valid:
            self._byte_error("bounded scanner chunks disagree with its actual return branch", frame, expected_id, inspection.owner)
            return
        inspection.branch = branch
        inspection.returned_size = arg
        if inspection.private_resource and not _private_size_sources_current():
            self._byte_error("private resource size changed its original source", frame, expected_id, inspection.owner)

    def _byte_profile(self, frame: FrameType, event: str, arg: object, expected_id: int | None) -> None:
        code = frame.f_code
        self._scanner_size_profile(frame, event, arg, expected_id)
        if code is _LOW_CODE_OBJECTS[2][0] and event == "call" and not _scanner_sources_current():
            self._byte_error("bounded scanner original source changed", frame, expected_id)
        if code is _CHUNK_REQUEST and event == "return":
            for pending in self._byte_pending_parses.pop(id(frame), ()):
                self._byte_error(
                    "strict parser has no bound native byte provenance", frame, expected_id, self.records[pending.record_ordinal - 1]
                )
            if frame.f_lasti in _CHUNK_SUCCESS_RETURNS:
                self._expect_private_size(frame, expected_id)
        if code is _LOAD_SNAPSHOT or code is _LOAD_CHUNKS:
            if event == "return":
                self._byte_sources.pop(id(frame), None)
                self._byte_private_payloads.pop(id(frame), None)
                expectation = self._byte_private_expected.pop(id(frame), None)
                if expectation is not None and (expectation.inspection is None or expectation.inspection.branch is None):
                    self._byte_error("private resource size invocation is missing or incomplete", frame, expected_id, expectation.owner)
            elif event == "c_call" and any(current_retention_stage_memo() is memo for memo in self.memos):
                site = _SNAPSHOT_DECODE_BYTES_SITE if code is _LOAD_SNAPSHOT else _CHUNK_DECODE_BYTES_SITE
                if self._at_site(frame, code, site) and type(arg) is BuiltinFunctionType and arg.__name__ == "decode":
                    receiver = arg.__self__
                    if type(receiver) is bytes and self._byte_carrier(expected_id, frame) is not None:
                        native = (id(receiver), len(receiver))
                        if code is _LOAD_CHUNKS and self._byte_private_payloads.pop(id(frame), None) != native:
                            self._byte_error("private chunk decode lacks its original bounded SQL payload", frame, expected_id)
                        else:
                            self._byte_sources[id(frame)] = native
        if code is _READ_PRIVATE_CHUNK_CODE and event == "return" and any(current_retention_stage_memo() is memo for memo in self.memos):
            parent = frame.f_back
            if (
                parent is not None
                and self._at_site(parent, _LOAD_CHUNKS, _CHUNK_READ_SITE)
                and continuation_selection.read_private_chunk is _READ_PRIVATE_CHUNK
                and stored_continuation.read_private_chunk is _READ_PRIVATE_CHUNK
                and _READ_PRIVATE_CHUNK.__code__ is _READ_PRIVATE_CHUNK_CODE
                and frame.f_globals is stored_continuation.__dict__
                and stored_continuation.sha256 is _SHA256
                and frame.f_locals.get("conn") is parent.f_locals.get("conn")
                and frame.f_locals.get("reference") is parent.f_locals.get("ref")
                and frame.f_locals.get("session_id") is parent.f_locals.get("sid")
                and frame.f_locals.get("operation_id") is parent.f_locals.get("operation_id")
                and type(arg) is bytes
                and self._byte_carrier(expected_id, frame) is not None
            ):
                self._byte_private_payloads[id(parent)] = (id(arg), len(arg))
        if event == "return" and code is _LOW_CODE_OBJECTS[2][0]:
            self._byte_detector_frames.pop(id(frame), None)
        if event == "return" and type(_RFC_DUMPS) is FunctionType and code is _RFC_DUMPS.__code__:
            parent = frame.f_back
            if parent is not None and parent.f_code is _CANONICAL and id(parent) in self._byte_canonical_owners and type(arg) is bytes:
                self._byte_canonical_buffers[id(parent)] = (id(arg), len(arg))
            elif any(current_retention_stage_memo() is memo for memo in self.memos):
                self._byte_error("RFC8785 work lacks its original canonical DATA owner", frame, expected_id)
            return
        if code is _CANONICAL:
            if event == "call":
                owner = self._canonical_byte_owner(frame)
                if owner is None:
                    if any(current_retention_stage_memo() is memo for memo in self.memos) or expected_id is not None:
                        self._byte_error("canonical work lacks its complete DATA owner", frame, expected_id)
                elif self._byte_carrier(expected_id, frame) is owner.memo:
                    if type(owner) is ContextCanonicalInspection and owner.expected_memo_id != expected_id:
                        self._byte_error("provider context canonical carrier changed during its invocation", frame, expected_id, owner)
                    else:
                        self._byte_canonical_owners[id(frame)] = owner
            elif event == "return":
                owner = self._byte_canonical_owners.pop(id(frame), None)
                native = self._byte_canonical_buffers.pop(id(frame), None)
                if owner is None:
                    return
                if self._byte_carrier(expected_id, frame) is not owner.memo:
                    return
                result = frame.f_locals.get("result")
                if (
                    type(arg) is not str
                    or type(result) is not bytes
                    or native != (id(result), len(result))
                    or hashing.rfc8785.dumps is not _RFC_DUMPS
                ):
                    self._byte_error("canonical output has no original RFC8785 buffer", frame, expected_id, owner)
                else:
                    owner.byte_calls["canonical"] += 1
                    owner.byte_counts["canonical"] += len(result)
            return
        record = self._ancestor(frame.f_back)
        if record is None:
            if code is _PARSE and event == "call" and any(current_retention_stage_memo() is memo for memo in self.memos):
                self._byte_error("strict parser lacks its complete DATA owner", frame, expected_id)
            return
        if code is not _PARSE:
            return
        if self._byte_carrier(expected_id, frame) is not record.memo:
            return
        if code is _PARSE and event == "call":
            size = self._parse_bytes(frame)
            if size is None:
                if not self._defer_chunk_parse(frame, record, expected_id):
                    self._byte_error("strict parser has no bound native byte provenance", frame, expected_id, record)
            else:
                record.byte_calls["strict_parse"] += 1
                record.byte_counts["strict_parse"] += size

    def _byte_call(self, code: CodeType, offset: int, callable_, operand) -> None:
        """Read only the calibrated original CALL operand, never reconstruct it."""
        frame = None
        try:
            frame = sys._getframe(1)
            if frame.f_code is not code or frame.f_lasti != offset:
                self._byte_error("native byte CALL lost its original frame or offset", frame, self._byte_threads.get(get_ident()))
                return
            if code in _SCANNER_SIZE_SITES:
                if offset in _SCANNER_SIZE_SITES[code]:
                    self._scanner_size_call(frame, offset, callable_, operand)
                return
            if code is _CHUNK_REQUEST:
                if callable_ is builtins.len:
                    channel = "chunk_length"
                    site = _CHUNK_LENGTH_SITE
                elif callable_ is _SHA256:
                    channel = "chunk_digest"
                    site = _CHUNK_DIGEST_SITE
                else:
                    return
                if not self._at_site(frame, _CHUNK_REQUEST, site):
                    self._byte_error("chunk binding lost its original callsite", frame, self._byte_threads.get(get_ident()))
                    return
                if operand is not frame.f_locals.get("encoded"):
                    self._byte_error("chunk binding operand differs from its native buffer", frame, self._byte_threads.get(get_ident()))
                    return
                owner = self._frames.get(id(frame))
            else:
                sites = _EXTENT_SITES.get(code)
                channel = "scanner_extent"
                if sites is None:
                    sites = _PREPARATION_SITES.get(code)
                    channel = "key_preparation"
                if sites is None or offset not in sites:
                    return
                if callable_ is not builtins.len:
                    self._byte_error("native encoded extent changed its original callable", frame, self._byte_threads.get(get_ident()))
                    return
                owner = None
                if channel == "key_preparation":
                    owner = self._ancestor(frame.f_back)
                else:
                    inspection = self._byte_size_frames.get(id(frame))
                    owner = None if inspection is None else inspection.owner
                    if inspection is not None:
                        chunk = frame.f_locals.get("chunk")
                        if (
                            not _scanner_sources_current()
                            or inspection.thread != get_ident()
                            or inspection.expected_memo_id != self._byte_threads.get(get_ident())
                            or frame.f_locals.get("start") != inspection.chunks * _UTF8_CHUNK_CHARACTERS
                            or frame.f_locals.get("size") != inspection.encoded_bytes
                            or type(chunk) is not str
                            or str.__len__(chunk)
                            != min(_UTF8_CHUNK_CHARACTERS, inspection.characters - inspection.chunks * _UTF8_CHUNK_CHARACTERS)
                        ):
                            self._byte_error(
                                "bounded scanner chunk lost its exact invocation or sequence",
                                frame,
                                self._byte_threads.get(get_ident()),
                                owner,
                            )
                            return
            actual = current_retention_stage_memo()
            # Local events also occur outside any observation stage. Only a
            # known live stage or a submitted observed thread is in scope.
            inflight = self._ancestor(frame.f_back) is not None or any(
                invocation.thread_id == get_ident() for invocation in self._validator_frames.values()
            )
            if actual is None and owner is None and self._byte_threads.get(get_ident()) is None and not inflight:
                return
            expected = self._byte_threads.get(get_ident())
            with self._lock:
                if self._byte_carrier(expected, frame) is None:
                    return
                if owner is None or owner.memo is not actual:
                    self._byte_error("native encoded extent lacks its actual charged owner", frame, expected, owner)
                    return
                if type(operand) is not bytes:
                    self._byte_error("native encoded extent is not exact bytes", frame, expected, owner)
                    return
                if code is _CHUNK_REQUEST:
                    self._bind_pending_chunk(frame, owner, operand, expected)
                elif code is _UTF8_CODE:
                    inspection.chunks += 1
                    inspection.encoded_bytes += len(operand)
                    if inspection.private_resource:
                        return
                owner.byte_calls[channel] += 1
                owner.byte_counts[channel] += len(operand)
        except Exception as error:
            self._byte_error(
                "native byte callback: " + type(error).__name__, frame or sys._getframe(1), self._byte_threads.get(get_ident())
            )

    def _event(self, frame: FrameType, event: str, arg: object, expected_id: int | None) -> None:
        code = frame.f_code
        high = code is _SNAPSHOT_REQUEST or code is _CHUNK_REQUEST or code is _CREDENTIAL_REQUEST or code is _STATE_REQUEST
        if event == "return":
            if high or code is _METADATA_VALIDATOR or code is _CONTEXT_VALIDATOR:
                with self._lock:
                    self._frames.pop(id(frame), None)
                    invocation = self._validator_frames.pop(id(frame), None)
                    context = self._context_byte_frames.pop(id(frame), None)
                    if context is not None:
                        context.exited = True
                    if invocation is not None:
                        # A profile return also accompanies a raised exception;
                        # native tests, not this flag, establish success/refusal.
                        invocation.exited = True
            return
        if event == "c_call":
            binding = None
            if arg is _SHA256:
                binding = "sha256"
            elif code is _CHUNK_REQUEST and arg is builtins.len:
                binding = "length"
            elif (
                code is _CHUNK_REQUEST
                and type(arg) is BuiltinFunctionType
                and arg.__name__ == "encode"
                and arg.__self__ is frame.f_locals.get("canonical")
            ):
                binding = "utf8"
            if binding is not None:
                with self._lock:
                    record = self._ancestor(frame, "chunk")
                    if record is not None:
                        record.attempts[binding] += 1
            return
        if event != "call" or not (
            high
            or code is _METADATA_VALIDATOR
            or code is _CONTEXT_VALIDATOR
            or id(code) in self._query_codes
            or id(code) in _LOW_CODES
            or code is _PARSE
            or code is _CANONICAL
        ):
            return
        actual = current_retention_stage_memo()
        with self._lock:
            if actual is None:
                if expected_id is not None and any(id(memo) == expected_id and memo.active for memo in self.memos):
                    self.errors.append("target work lacked its actual stage carrier")
                elif any(record.thread_id == get_ident() for record in self._validator_frames.values()):
                    self.errors.append("uncached validation lost its actual stage carrier")
                return
            if not any(actual is memo for memo in self.memos):
                if (expected_id is not None and any(id(memo) == expected_id and memo.active for memo in self.memos)) or any(
                    record.thread_id == get_ident() for record in self._validator_frames.values()
                ):
                    self.errors.append("target work carried an unobserved stage")
                return
            if code is _METADATA_VALIDATOR or code is _CONTEXT_VALIDATOR:
                if code is _METADATA_VALIDATOR and type(frame.f_locals.get("self")) is not diagnostic_evidence.ComposerDiagnosticEvidence:
                    return
                if expected_id is not None and expected_id != id(actual):
                    # Refusals can exit before their first detector call.
                    # Invocation custody must not depend on later work.
                    self.errors.append("uncached validation disagreed with its submitted stage carrier")
                    return
                invocation = UncachedValidationInvocation(
                    actual,
                    len(self.uncached_validations) + 1,
                    get_ident(),
                    validator=_METADATA_VALIDATOR_NAME if code is _METADATA_VALIDATOR else _CONTEXT_VALIDATOR_NAME,
                    expected_memo_id=expected_id,
                )
                self.uncached_validations.append(invocation)
                self._validator_frames[id(frame)] = invocation
                if self.require_native_bytes and code is _CONTEXT_VALIDATOR:
                    context = ContextCanonicalInspection(
                        actual,
                        sum(record.memo is actual for record in self.context_canonical_inspections) + 1,
                        get_ident(),
                        expected_id,
                    )
                    self.context_canonical_inspections.append(context)
                    self._context_byte_frames[id(frame)] = context
                return
            if high:
                family = "snapshot" if code is _SNAPSHOT_REQUEST else "chunk" if code is _CHUNK_REQUEST else "credential"
                inherited = self._ancestor(frame.f_back)
                if family == "credential" and inherited is not None and inherited.family in ("credential", "state"):
                    self._frames[id(frame)] = inherited
                else:
                    record = MemoInspection(family, actual)
                    self.records.append(record)
                    self._frames[id(frame)] = record
                return
            record = self._ancestor(frame.f_back)
            if id(code) in self._query_codes:
                key = frame.f_locals.get("key")
                producer = frame.f_locals.get("producer")
                if record is not None and type(producer) is FunctionType and _safe_key(key):
                    expected_family = (
                        "snapshot"
                        if type(key) is SnapshotDecodeKey
                        else "chunk"
                        if type(key) is PrivateChunkVerificationKey
                        else "credential"
                    )
                    if record.family not in (expected_family, "state"):
                        self.errors.append("memo query lacks its actual family request")
                    else:
                        record.key, record.producer = key, producer
                        if type(key) is StateCredentialStepKey:
                            record.family = "state"
                return
            if code is _PARSE or code is _CANONICAL:
                if record is not None and record.family == "chunk" and not (code is _CANONICAL and self._original_context_canonical(frame)):
                    record.attempts["strict_parse" if code is _PARSE else "canonical_encode"] += 1
                return
            label = _LOW_CODES[id(code)]
            if label == "detector_work":
                self.detector_attempts[actual] += 1
                if self._uncached_metadata_attempt(frame, actual, expected_id):
                    if self.require_native_bytes:
                        self._byte_detector_frames[id(frame)] = self._validator_frames[id(frame.f_back)]
                    return
                if self._uncached_context_attempt(frame, actual, expected_id):
                    if self.require_native_bytes:
                        ancestor = frame.f_back
                        for _ in range(3):
                            assert ancestor is not None
                            ancestor = ancestor.f_back
                        self._byte_detector_frames[id(frame)] = self._validator_frames[id(ancestor)]
                    return
                if self._uncached_validator_ancestor(frame.f_back):
                    # A changed validator path/policy cannot borrow an outer
                    # memo request to turn failed uncached attribution green.
                    record = None
            expected_families = (
                ("snapshot",) if label == "snapshot_work" else ("chunk",) if label == "chunk_work" else ("credential", "state")
            )
            if record is None or record.family not in expected_families:
                self.orphans.append((id(actual), label, code.co_filename, code.co_firstlineno))
                if self.first_orphan is None:
                    self.first_orphan = _orphan_diagnostic(frame, id(actual), label, expected_id, record)
            else:
                record.attempts[label] += 1
                if label == "detector_work":
                    record.actual_policies.append(_policy_data(frame.f_locals.get("policy")))
                    if self.require_native_bytes:
                        self._byte_detector_frames[id(frame)] = record

    def _uncached_validator_ancestor(self, frame: FrameType | None) -> bool:
        for _ in range(64):
            if frame is None:
                return False
            if frame.f_code is _METADATA_VALIDATOR or frame.f_code is _CONTEXT_VALIDATOR:
                return True
            frame = frame.f_back
        return False

    def _uncached_metadata_attempt(self, frame: FrameType, actual: RetentionStageMemo, expected_id: int | None) -> bool:
        caller = frame.f_back
        if (
            caller is None
            or caller.f_code is not _METADATA_VALIDATOR
            or caller.f_lasti not in _METADATA_CALL_OFFSETS
            or caller.f_lineno != _METADATA_CALL_LINE
            or (expected_id is not None and expected_id != id(actual))
        ):
            return False
        invocation = self._validator_frames.get(id(caller))
        policy = frame.f_locals.get("policy")
        if (
            invocation is None
            or invocation.memo is not actual
            or invocation.thread_id != get_ident()
            or invocation.expected_memo_id != expected_id
            or policy is not _METADATA_POLICY
            or diagnostic_evidence._POLICY is not _METADATA_POLICY
        ):
            return False
        policy_key = _policy_data(policy)
        value = frame.f_locals.get("value")
        if policy_key is None or policy_key != _METADATA_POLICY_KEY or type(value) is not str or not 0 < len(value) <= 512:
            return False
        invocation.attempts.append(
            UncachedValidationAttempt(
                MemoCallSite(caller.f_code.co_filename, caller.f_code.co_qualname, caller.f_code.co_firstlineno, caller.f_lineno),
                id(policy),
                policy_key,
                "str",
                len(value),
            )
        )
        return True

    def _uncached_context_attempt(self, frame: FrameType, actual: RetentionStageMemo, expected_id: int | None) -> bool:
        if expected_id is not None and expected_id != id(actual):
            return False
        ancestry = []
        ancestor = frame.f_back
        for code, (offsets, line) in (
            (_CONTEXT_TEXT, _CONTEXT_TEXT_SITE),
            (_CONTEXT_CONTENT, _CONTEXT_CONTENT_SITE),
            (_CONTEXT_MESSAGES, _CONTEXT_MESSAGES_SITE),
            (_CONTEXT_VALIDATOR, _CONTEXT_CAPTURE_SITE),
        ):
            if ancestor is None or ancestor.f_code is not code or ancestor.f_lasti not in offsets or ancestor.f_lineno != line:
                return False
            ancestry.append(MemoCallSite(code.co_filename, code.co_qualname, code.co_firstlineno, ancestor.f_lineno))
            capture_frame_id = id(ancestor)
            ancestor = ancestor.f_back
        invocation = self._validator_frames.get(capture_frame_id)
        policy = frame.f_locals.get("policy")
        if (
            invocation is None
            or invocation.validator != _CONTEXT_VALIDATOR_NAME
            or invocation.memo is not actual
            or invocation.thread_id != get_ident()
            or invocation.expected_memo_id != expected_id
            or policy is not _CONTEXT_POLICY
            or provider_context._POLICY is not _CONTEXT_POLICY
        ):
            return False
        policy_key = _policy_data(policy)
        value = frame.f_locals.get("value")
        if policy_key is None or policy_key != _CONTEXT_POLICY_KEY or type(value) is not str:
            return False
        # Original capture has already enforced aggregate JSON admission. This
        # measures only the observed scalar's character length, including zero.
        invocation.attempts.append(UncachedValidationAttempt(ancestry[0], id(policy), policy_key, "str", len(value), tuple(ancestry)))
        return True

    def uncached_validation_totals(self) -> tuple[UncachedValidationTotals, ...]:
        totals = []
        for stage, memo in enumerate(self.memos, 1):
            for validator in _UNCACHED_VALIDATORS:
                invocations = [record for record in self.uncached_validations if record.memo is memo and record.validator == validator]
                if invocations:
                    attempts = [attempt for record in invocations for attempt in record.attempts]
                    totals.append(
                        UncachedValidationTotals(
                            stage, validator, len(invocations), len(attempts), sum(attempt.input_characters for attempt in attempts)
                        )
                    )
        return tuple(totals)

    def _byte_rows(self):
        rows = []
        for stage, memo in enumerate(self.memos, 1):
            groups = []
            for record in self.records:
                if record.memo is memo and record.group is not None and record.group not in groups:
                    groups.append(record.group)
            for group in groups:
                calls = [record for record in self.records if record.memo is memo and record.group == group]
                rows.append((stage, calls[0].family, group[2], calls[0].producer, None, calls))
            for validator in _UNCACHED_VALIDATORS:
                calls = [record for record in self.uncached_validations if record.memo is memo and record.validator == validator]
                if calls:
                    rows.append((stage, validator, None, None, None, calls))
            for record in self.context_canonical_inspections:
                if record.memo is memo:
                    rows.append((stage, _CONTEXT_CANONICAL_NAME, None, _CAPTURE_CONTEXT, record.invocation, [record]))
        return rows

    def native_byte_totals(self) -> tuple[NativeByteTotals, ...]:
        if not self.require_native_bytes:
            return ()
        return tuple(
            NativeByteTotals(
                stage,
                family,
                group,
                channel,
                sum(record.byte_calls[channel] for record in records),
                sum(record.byte_counts[channel] for record in records),
                invocation,
            )
            for group, (stage, family, _key, _producer, invocation, records) in enumerate(self._byte_rows(), 1)
            for channel in _BYTE_CHANNELS
        )

    def _private_size_state(self) -> tuple[tuple, ...]:
        rows = tuple(
            (
                id(item.owner),
                item.loader,
                item.reference,
                item.text,
                item.characters,
                item.byte_length,
                item.thread,
                item.expected_memo_id,
                item.calls,
                None
                if item.inspection is None
                else (
                    id(item.inspection.owner),
                    item.inspection.caller,
                    item.inspection.text,
                    item.inspection.characters,
                    item.inspection.limit,
                    item.inspection.thread,
                    item.inspection.expected_memo_id,
                    item.inspection.call_code,
                    item.inspection.call_offset,
                    item.inspection.profile_offset,
                    item.inspection.private_resource,
                    item.inspection.chunks,
                    item.inspection.encoded_bytes,
                    item.inspection.branch,
                    item.inspection.returned_size,
                ),
            )
            for item in self._byte_private_sizes
        )
        return (("requests", tuple(sorted(self._byte_private_requests.items()))), *rows)

    def _assert_private_size_work(self) -> None:
        assert not self._byte_private_expected, "private resource size work remains active"
        assert (
            self._byte_private_requests == Counter(id(item.owner) for item in self._byte_private_sizes)
            and all(count == 1 for count in self._byte_private_requests.values())
            and set(self._byte_private_requests).issubset(id(record) for record in self.records)
        ), "private resource size lost or duplicated its finite request charge"
        owners = set()
        for item in self._byte_private_sizes:
            inspection = item.inspection
            assert item.calls == 1 and inspection is not None and id(item.owner) not in owners, (
                "private resource size lost or duplicated its finite request charge"
            )
            owners.add(id(item.owner))
            key = item.owner.key
            assert (
                type(key) is PrivateChunkVerificationKey
                and id(key.reference) == item.reference
                and id(key.canonical) == item.text
                and key.reference.byte_length == item.byte_length
                and item.owner.byte_calls["chunk_length"] == item.owner.byte_calls["chunk_digest"] == 1
                and item.owner.byte_counts["chunk_length"] == item.owner.byte_counts["chunk_digest"] == item.byte_length
                and inspection.owner is item.owner
                and inspection.text == item.text
                and inspection.characters == item.characters
                and inspection.limit == _PRIVATE_UTF8_LIMIT
                and inspection.thread == item.thread
                and inspection.expected_memo_id == item.expected_memo_id
                and inspection.call_code is inspection.call_offset is inspection.profile_offset is None
                and inspection.private_resource
                and inspection.returned_size == item.byte_length
                and (
                    (
                        inspection.branch == "ascii"
                        and inspection.chunks == inspection.encoded_bytes == 0
                        and item.characters == item.byte_length
                    )
                    or (
                        inspection.branch == "encoded"
                        and inspection.encoded_bytes == item.byte_length
                        and inspection.chunks == (item.characters + _UTF8_CHUNK_CHARACTERS - 1) // _UTF8_CHUNK_CHARACTERS
                    )
                )
            ), "private resource size changed or exchanged its bounded DATA work"

    def _assert_native_byte_budget(self, budgets: tuple[NativeByteBudget, ...]) -> None:
        assert not self.byte_errors, "native byte provenance invalid: " + str(self.byte_errors)
        assert self._byte_monitor is not None and self._byte_monitor.closed, "native byte monitoring remains active"
        assert (
            not self._byte_detector_frames
            and not self._byte_size_pending
            and not self._byte_size_frames
            and all(inspection.branch is not None for inspection in self._byte_size_inspections)
            and not self._byte_canonical_buffers
            and not self._byte_canonical_owners
            and not self._byte_sources
            and not self._byte_private_payloads
            and not self._context_byte_frames
            and not self._byte_pending_parses
            and all(record.exited for record in self.context_canonical_inspections)
        ), "native byte work remains active"
        self._assert_private_size_work()
        assert all(
            item.call_code is not None
            and type(item.call_offset) is int
            and type(item.profile_offset) is int
            and item.call_code in _SCANNER_SIZE_WINDOWS
            and item.call_offset in _SCANNER_SIZE_WINDOWS[item.call_code]
            and item.profile_offset in _SCANNER_SIZE_WINDOWS[item.call_code][item.call_offset][0]
            for item in self._byte_size_inspections
        ), "bounded scanner lost its original native CALL origin"
        for _stage, _family, _key, _producer, _invocation, records in self._byte_rows():
            for record in records:
                inspections = [inspection for inspection in self._byte_size_inspections if inspection.owner is record]
                assert (
                    record.byte_calls["scanner_size"] == len(inspections)
                    and record.byte_counts["scanner_size"] == 0
                    and record.byte_calls["scanner_extent"] == sum(inspection.chunks for inspection in inspections)
                    and record.byte_counts["scanner_extent"] == sum(inspection.encoded_bytes for inspection in inspections)
                ), "bounded scanner lost or duplicated its invocation or chunk charge"
        assert type(budgets) is tuple and all(type(budget) is NativeByteBudget for budget in budgets)
        rows = self._byte_rows()
        identities = [(stage, family, key, producer, invocation) for stage, family, key, producer, invocation, _records in rows]
        actual = []
        for budget in budgets:
            assert type(budget.stage) is int and budget.stage >= 1 and type(budget.family) is str
            if budget.family == _CONTEXT_CANONICAL_NAME:
                assert (
                    budget.key is None and budget.producer is _CAPTURE_CONTEXT and type(budget.invocation) is int and budget.invocation >= 1
                ), "native byte capture budget lost its complete original invocation"
            elif budget.family in _UNCACHED_VALIDATORS:
                assert budget.key is None and budget.producer is None, "native byte validator budget acquired a DATA key"
                assert budget.invocation is None, "scalar validator budget acquired a capture identity"
            else:
                assert _safe_key(budget.key) and type(budget.producer) is FunctionType, "native byte budget lost its complete DATA key"
                assert budget.invocation is None, "memo DATA budget acquired a capture identity"
            identity = (budget.stage, budget.family, budget.key, budget.producer, budget.invocation)
            assert identity not in actual, "duplicate native byte budget"
            actual.append(identity)
            assert type(budget.limits) is tuple and all(type(limit) is NativeByteLimit for limit in budget.limits)
            channels = [limit.channel for limit in budget.limits]
            assert len(channels) == len(set(channels)) and set(channels) == set(_BYTE_CHANNELS), (
                "native byte channels are required exactly once"
            )
            assert identity in identities, "native byte budget has the wrong stage, family, source or DATA key"
            records = rows[identities.index(identity)][5]
            for limit in budget.limits:
                assert all(type(value) is int and value >= 0 for value in (limit.min_calls, limit.max_calls, limit.max_bytes))
                assert limit.min_calls <= limit.max_calls, "native byte call bounds are inverted"
                calls = sum(record.byte_calls[limit.channel] for record in records)
                size = sum(record.byte_counts[limit.channel] for record in records)
                assert limit.min_calls <= calls <= limit.max_calls, "native byte call budget exceeded or missing: " + limit.channel
                assert 0 <= size <= limit.max_bytes, "native byte extent budget exceeded: " + limit.channel
        assert len(actual) == len(identities) and all(identity in actual for identity in identities), "native byte budget is required"
        assert all(not (record.byte_calls or record.byte_counts) or record.group is not None for record in self.records), (
            "native bytes lack a complete DATA key"
        )
        self._verified_byte_totals = self.native_byte_totals()

    def detector_stage_totals(self) -> tuple[DetectorStageTotals, ...]:
        return tuple(
            DetectorStageTotals(
                stage,
                self.detector_attempts[memo],
                sum(record.attempts["detector_work"] for record in self.records if record.memo is memo),
                sum(len(record.attempts) for record in self.uncached_validations if record.memo is memo),
                sum(memo_id == id(memo) and label == "detector_work" for memo_id, label, _path, _line in self.orphans),
            )
            for stage, memo in enumerate(self.memos, 1)
        )

    def _assert_uncached_budget(self, limits: tuple[UncachedValidationBudget, ...]) -> None:
        totals = self.uncached_validation_totals()
        assert type(limits) is tuple and all(type(limit) is UncachedValidationBudget for limit in limits)
        assert all(
            type(limit.stage) is int and limit.stage >= 1 and type(limit.validator) is str and limit.validator in _UNCACHED_VALIDATORS
            for limit in limits
        ), "uncached validation budget lost its stage or validator"
        keys = [(limit.stage, limit.validator) for limit in limits]
        assert len(keys) == len(set(keys)), "duplicate uncached validation budget"
        assert set(keys) == {(total.stage, total.validator) for total in totals}, "uncached validation budget is required"
        assert not self._validator_frames and all(record.exited for record in self.uncached_validations), "uncached validation still active"
        assert all(
            len(record.attempts) <= 6 and all(0 < attempt.input_characters <= 512 for attempt in record.attempts)
            for record in self.uncached_validations
            if record.validator == _METADATA_VALIDATOR_NAME
        ), "diagnostic metadata invocation exceeded six scans"
        for limit in limits:
            assert all(
                type(value) is int and value >= 0 for value in (limit.max_invocations, limit.max_attempts, limit.max_input_characters)
            ), "uncached validation budgets must be finite nonnegative integers"
            total = next(total for total in totals if (total.stage, total.validator) == (limit.stage, limit.validator))
            assert total.attempts <= limit.max_attempts, "uncached validation attempt budget exceeded"
            assert total.invocations <= limit.max_invocations, "uncached validation invocation budget exceeded"
            assert total.input_characters <= limit.max_input_characters, "uncached validation scalar-input budget exceeded"
        self._verified_uncached_totals = totals

    def _close_report_registration(self) -> None:
        manager = self._report_manager
        if manager is None or get_ident() != self._owner_thread:
            return
        current = manager.get_plugin(self._report_name)
        if current is self:
            if manager.unregister(self) is not self or manager.get_plugin(self._report_name) is not None:
                self._observation_incomplete = True
                self.errors.append("report cleanup registration was not released")
                return
        elif self._report_registered or current is not None:
            self._observation_incomplete = True
            self.errors.append("report cleanup registration changed during observation")
        self._report_registered = False
        self._report_manager = None
        self._report_item = None

    def close(self, *, primary: BaseException | None = None) -> None:
        primary = sys.exception() if primary is None else primary
        if (
            self._closed
            and not (self._installed and get_ident() == self._owner_thread)
            and self._report_manager is None
            and (self._byte_monitor is None or self._byte_monitor.closed)
        ):
            return
        # No observer/application lock: close can run inside an observed
        # scope, worker, or original exception path. Later inactivity cannot
        # repair the missing coverage after this boundary.
        if (
            not self._closed
            and self.require_catalog_schema
            and (
                self._catalog_schema_scopes
                or self._catalog_schema_frames
                or len(self._owned_hooks) > 1
                or len(self._byte_threads) > 1
                or any(memo._closed is not True for memo in self.memos)
            )
        ):
            self._catalog_schema_error("catalog schema observation closed before its stage or worker exited")
        interrupt = None
        if self._structure_required:
            try:
                self._structure_close(primary)
            except BaseException as error:
                interrupt = error
                self._structure_observer_incomplete = True
        self._closed = True
        if self._installed and get_ident() == self._owner_thread:
            try:
                if not self._main_state.prior_failed:
                    if sys.getprofile() is self._main_hook:
                        sys.setprofile(self._previous)
                    else:
                        self.errors.append("loop profiler changed during observation")
                self._installed = False
            except BaseException as error:
                self._observation_incomplete = True
                self.errors.append("loop profile cleanup interrupted")
                if interrupt is None:
                    interrupt = error
        if self._byte_monitor is not None:
            try:
                self._byte_monitor.close(primary)
            except BaseException as error:
                self._observation_incomplete = True
                self.errors.append("byte monitor cleanup interrupted")
                if interrupt is None:
                    interrupt = error
        try:
            self._close_report_registration()
        except BaseException as error:
            self._observation_incomplete = True
            self.errors.append("report cleanup registration interrupted")
            if interrupt is None:
                interrupt = error
        self._structure_row_codes.clear()
        if interrupt is not None and primary is None:
            raise interrupt

    def _finalize(self) -> None:
        self.close()
        assert self._finished, "memo observation window was not explicitly finalized"
        assert tuple(self.errors) == self._terminal_errors, "observer changed after its terminal assertion"
        assert self._observation_incomplete is self._terminal_incomplete, "observation changed after its terminal assertion"
        if self._structure_required:
            assert self._structure_state() == self._terminal_structure_state, "structure changed after its terminal assertion"
        assert self.uncached_validation_totals() == self._terminal_uncached_totals, "uncached work changed after its terminal assertion"
        if self.require_native_bytes:
            assert tuple(self.byte_errors) == self._terminal_byte_errors, "native byte errors changed after terminal assertion"
            assert tuple(self.byte_error_diagnostics) == self._terminal_byte_diagnostics, (
                "native byte diagnostics changed after terminal assertion"
            )
            assert not self._byte_pending_parses, "native parse bindings remain after terminal assertion"
            assert self.native_byte_totals() == self._terminal_byte_totals, "native byte work changed after terminal assertion"
            assert self._private_size_state() == self._terminal_private_sizes, "private resource size work changed after terminal assertion"
        if self.require_snapshot_artifacts:
            assert not self._snapshot_artifact_requests, "snapshot artifact work remains active after terminal assertion"
            assert tuple(self.snapshot_artifact_errors) == self._terminal_snapshot_artifact_errors, (
                "snapshot artifact errors changed after terminal assertion"
            )
            assert self.snapshot_artifact_totals() == self._terminal_snapshot_artifact_rows, (
                "snapshot artifact metrics changed after terminal assertion"
            )
            assert self.snapshot_artifact_stage_totals() == self._terminal_snapshot_artifact_stages, (
                "snapshot artifact stages changed after terminal assertion"
            )
            assert self._snapshot_artifact_verified is self._terminal_snapshot_artifact_verified, (
                "snapshot artifact qualification changed after terminal assertion"
            )
        if self.require_chunk_artifacts:
            assert not self._chunk_artifact_requests, "chunk artifact work remains active after terminal assertion"
            assert tuple(self.chunk_artifact_errors) == self._terminal_chunk_artifact_errors, (
                "chunk artifact errors changed after terminal assertion"
            )
            assert self.chunk_artifact_totals() == self._terminal_chunk_artifact_rows, (
                "chunk artifact metrics changed after terminal assertion"
            )
            assert self.chunk_artifact_stage_totals() == self._terminal_chunk_artifact_stages, (
                "chunk artifact stages changed after terminal assertion"
            )
            assert self._chunk_artifact_verified is self._terminal_chunk_artifact_verified, (
                "chunk artifact qualification changed after terminal assertion"
            )
        if self.require_catalog_schema:
            assert not self._catalog_schema_frames and not self._catalog_schema_scopes, (
                "catalog schema work remains active after terminal assertion"
            )
            assert self._catalog_schema_state() == self._terminal_catalog_schema_state, (
                "catalog schema state changed after terminal assertion"
            )
            assert not self._catalog_schema_verified or self._catalog_schema_sources_current(), (
                "catalog schema source changed after terminal assertion"
            )

    def _reset_artifact_qualification(self) -> None:
        if self.require_snapshot_artifacts:
            self._snapshot_artifact_verified = False
            self._terminal_snapshot_artifact_verified = False
        if self.require_chunk_artifacts:
            self._chunk_artifact_verified = False
            self._terminal_chunk_artifact_verified = False

    def finish(self, **kwargs) -> None:
        primary = sys.exception()
        self._finished = True
        try:
            self.close()
        except BaseException:
            if not self._structure_required:
                raise
            # Cleanup already tried every owned listener and restored the
            # loop profiler. Freeze an explicitly unqualified terminal state
            # before the identical interrupt escapes; finalization must not
            # manufacture a second failure from missing terminal bookkeeping.
            self._reset_artifact_qualification()
            self._structure_observation_verified = False
            self._terminal_errors = tuple(self.errors)
            self._terminal_incomplete = self._observation_incomplete
            self._terminal_uncached_totals = self.uncached_validation_totals()
            self._terminal_byte_totals = self.native_byte_totals()
            self._terminal_byte_errors = tuple(self.byte_errors)
            self._terminal_byte_diagnostics = tuple(self.byte_error_diagnostics)
            self._terminal_snapshot_artifact_errors = tuple(self.snapshot_artifact_errors)
            self._terminal_snapshot_artifact_rows = self.snapshot_artifact_totals()
            self._terminal_snapshot_artifact_stages = self.snapshot_artifact_stage_totals()
            self._terminal_chunk_artifact_errors = tuple(self.chunk_artifact_errors)
            self._terminal_chunk_artifact_rows = self.chunk_artifact_totals()
            self._terminal_chunk_artifact_stages = self.chunk_artifact_stage_totals()
            if self.require_catalog_schema:
                self._catalog_schema_verified = False
                self._terminal_catalog_schema_state = self._catalog_schema_state()
            self._terminal_structure_state = self._structure_state()
            raise
        if self._structure_required:
            self._structure_observation_verified = False
            self._terminal_structure_state = self._structure_state()
        if self.require_catalog_schema:
            self._catalog_schema_verified = False
            if not self._catalog_schema_sources_current():
                self._catalog_schema_source_valid = False
                self._catalog_schema_error("catalog schema original source changed at closure")
        self._reset_artifact_qualification()
        if self.require_snapshot_artifacts:
            self._size_snapshot_artifacts()
        if self.require_chunk_artifacts:
            self._size_chunk_artifacts()
        self._terminal_errors = tuple(self.errors)
        self._terminal_incomplete = self._observation_incomplete
        self._terminal_uncached_totals = self.uncached_validation_totals()
        self._terminal_byte_totals = self.native_byte_totals()
        self._terminal_byte_errors = tuple(self.byte_errors)
        self._terminal_byte_diagnostics = tuple(self.byte_error_diagnostics)
        self._terminal_private_sizes = self._private_size_state()
        self._terminal_snapshot_artifact_errors = tuple(self.snapshot_artifact_errors)
        self._terminal_snapshot_artifact_rows = self.snapshot_artifact_totals()
        self._terminal_snapshot_artifact_stages = self.snapshot_artifact_stage_totals()
        self._terminal_snapshot_artifact_verified = self._snapshot_artifact_verified
        self._terminal_chunk_artifact_errors = tuple(self.chunk_artifact_errors)
        self._terminal_chunk_artifact_rows = self.chunk_artifact_totals()
        self._terminal_chunk_artifact_stages = self.chunk_artifact_stage_totals()
        self._terminal_chunk_artifact_verified = self._chunk_artifact_verified
        if self.require_catalog_schema:
            self._terminal_catalog_schema_state = self._catalog_schema_state()
            if primary is not None:
                # The original native outcome continues through its finally.
                # Measurement is retained, but no requested budget is qualified.
                return
        if primary is not None and self._structure_required:
            return
        if primary is not None and (
            (self.require_snapshot_artifacts and self._snapshot_artifact_sizing_failed)
            or (self.require_chunk_artifacts and self._chunk_artifact_sizing_failed)
        ):
            # Refused measurement remains explicit/unqualified. Do not replace
            # the native error/cancellation already propagating through finally.
            return
        try:
            self.assert_closed(**kwargs)
            self.assert_complete_coverage()
        except BaseException:
            self._reset_artifact_qualification()
            if self._structure_required:
                self._structure_observation_verified = False
                self._terminal_structure_state = self._structure_state()
            if self.require_catalog_schema:
                self._catalog_schema_verified = False
                self._terminal_catalog_schema_state = self._catalog_schema_state()
            raise
        self._terminal_snapshot_artifact_verified = self._snapshot_artifact_verified
        self._terminal_chunk_artifact_verified = self._chunk_artifact_verified
        if self._structure_required:
            self._terminal_structure_state = self._structure_state()
        if self.require_catalog_schema:
            self._terminal_catalog_schema_state = self._catalog_schema_state()

    def assert_closed(
        self,
        *,
        stages: int,
        require_snapshot: bool = True,
        require_population: bool = True,
        require_valid_chunk_bindings: bool = True,
        uncached_validation_limits: tuple[UncachedValidationBudget, ...] = (),
        native_byte_limits: tuple[NativeByteBudget, ...] = (),
        snapshot_artifact_budget: bool = False,
        chunk_artifact_budget: bool = False,
        catalog_schema_budget: bool = False,
        structure_observation: bool = False,
    ) -> None:
        assert not self._observation_incomplete, "prior profiler failed; observation incomplete"
        assert not self.errors, self.errors
        self._assert_structure_observation(structure_observation)
        if self.require_native_bytes:
            self._assert_native_byte_budget(native_byte_limits)
        else:
            assert not native_byte_limits, "native byte budgets require explicit observation mode"
        assert type(snapshot_artifact_budget) is bool, "snapshot artifact assertion requires an explicit boolean"
        if self.require_snapshot_artifacts:
            self._assert_snapshot_artifact_budget(snapshot_artifact_budget)
        else:
            assert not snapshot_artifact_budget, "snapshot artifact budgets require explicit observation mode"
        assert type(chunk_artifact_budget) is bool, "chunk artifact assertion requires an explicit boolean"
        if self.require_chunk_artifacts:
            self._assert_chunk_artifact_budget(chunk_artifact_budget)
        else:
            assert not chunk_artifact_budget, "chunk artifact budgets require explicit observation mode"
        assert type(catalog_schema_budget) is bool, "catalog schema assertion requires an explicit boolean"
        if self.require_catalog_schema:
            self._assert_catalog_schema_budget(catalog_schema_budget)
        else:
            assert not catalog_schema_budget, "catalog schema budgets require explicit observation mode"
        assert not self.orphans, "unattributed actual memo work: " + str(self.orphans)
        assert len(self.memos) == stages and len(set(self.memos)) == stages
        assert current_retention_stage_memo() is None
        for memo in self.memos:
            assert not memo.active and memo.cached_entry_count == 0
            if require_population:
                assert self.populated[memo] > 0
            records = [record for record in self.records if record.memo is memo]
            if require_snapshot:
                snapshots = [record for record in records if record.family == "snapshot"]
                assert snapshots, "actual retention must request snapshot work"
                assert any(
                    sum(other.group == record.group for other in snapshots) > 1 for record in snapshots if record.group is not None
                ), "actual retention must exercise a warm hit"
            for group in (group for group in self.published if group[0] == id(memo)):
                calls = [record for record in records if record.group == group]
                assert calls, "published memo key lacks an observed request"
                attempts = sum((record.attempts for record in calls), Counter())
                if calls[0].family == "snapshot":
                    assert attempts["snapshot_work"] == 1, "snapshot input recomputed within one actual stage"
                elif calls[0].family == "chunk":
                    assert attempts["chunk_work"] == attempts["strict_parse"] == attempts["canonical_encode"] == 1, (
                        "chunk input reparsed within one actual stage"
                    )
                    if require_valid_chunk_bindings:
                        assert all(
                            record.attempts["utf8"] == record.attempts["length"] == record.attempts["sha256"] == 1 for record in calls
                        ), "valid chunk call omitted or repeated its real binding work"
                else:
                    assert attempts["detector_work"] == 1, "credential input recomputed within one actual stage"
                    scan = group[2].scan if type(group[2]) is StateCredentialStepKey else group[2]
                    assert all(policy == scan.policy for record in calls for policy in record.actual_policies), (
                        "actual detector policy differs from the DATA key"
                    )
        self._assert_uncached_budget(uncached_validation_limits)

    def assert_complete_coverage(self) -> None:
        assert not self._observation_incomplete, "prior profiler failed; observation incomplete"
        assert not self.errors, self.errors
        assert not self.orphans, self.orphans
        assert not self.excluded, "excluded submissions are not observed work: " + str(self.excluded)
        assert all(
            total.native_attempts == total.memo_family_attempts + total.uncached_validation_attempts + total.orphan_attempts
            for total in self.detector_stage_totals()
        ), "actual detector attempts lost their accounting charge"
        if self.uncached_validations:
            assert self._verified_uncached_totals == self.uncached_validation_totals(), "uncached validation lacks a verified budget"
        if self.require_native_bytes:
            assert not self.byte_errors, self.byte_errors
            assert self._verified_byte_totals == self.native_byte_totals(), "native bytes lack a verified budget"
        if self.require_snapshot_artifacts:
            assert not self.snapshot_artifact_errors, self.snapshot_artifact_errors
            assert self._snapshot_artifact_verified, "snapshot artifact lacks a verified budget"
        if self.require_chunk_artifacts:
            assert not self.chunk_artifact_errors, self.chunk_artifact_errors
            assert self._chunk_artifact_verified, "chunk artifact lacks a verified budget"
        if self.require_catalog_schema:
            assert self.catalog_schema_complete() and self._catalog_schema_verified, "catalog schema lacks a verified budget"
        if self._structure_required:
            assert self.structure_snapshot().complete and self._structure_observation_verified, "structure observation incomplete"


# Captured once after the original class is defined, before any Trace setup.
# Eligibility checks inspect the actual owned hook closure against these anchors.
_LIFETIME_PROFILE_FACTORY = RetentionMemoTrace._make_hook
_LIFETIME_PROFILE_FACTORY_CODE = _LIFETIME_PROFILE_FACTORY.__code__
_LIFETIME_PROFILE_CODE = next(
    code for code in _LIFETIME_PROFILE_FACTORY_CODE.co_consts if type(code) is CodeType and code.co_name == "hook"
)
_LIFETIME_PROFILE_FREEVARS = _LIFETIME_PROFILE_CODE.co_freevars
_LIFETIME_PROFILE_CELL_INDEXES = tuple(_LIFETIME_PROFILE_FREEVARS.index(name) for name in ("previous", "self", "state"))
