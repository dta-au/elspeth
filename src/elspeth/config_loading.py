"""Application-level pipeline settings loading and plugin declaration admission.

Core owns settings schemas and pure transformations. This module composes
those with plugin declarations before any environment expansion can turn
operator placeholders into output data.
"""

from collections.abc import Mapping
from pathlib import Path
from typing import Any, get_args, get_origin

import yaml
from pydantic import BaseModel

from elspeth.contracts.emitted_option import emitted_option_fields, env_placeholders_in
from elspeth.contracts.trust_boundary import trust_boundary
from elspeth.core.config import (
    _DYNACONF_INTERNAL_KEYS,
    ElspethSettings,
    _expand_config_templates,
    _expand_env_vars,
    _lower_llm_profile_nodes,
    _lowercase_schema_keys,
    _reject_file_backed_template_options_for_in_memory_loader,
    load_bounded_pipeline_yaml,
)


def _plugin_bearing_sections() -> dict[str, str]:
    """Return ``{section_name: container_shape}`` for every plugin-bearing section.

    DERIVED from ``ElspethSettings`` rather than listed. A section is
    plugin-bearing when its entries carry both ``plugin`` and ``options``; the
    shape is ``"dict"`` (name -> entry) or ``"list"``.

    The previous hand-written tuple omitted whichever section was added last —
    ``collectors`` first (elspeth-bc1b2c2959), and ``sources``/``sinks``
    permanently, on a registry argument that ``csv.headers`` falsified
    (elspeth-8f0a6b3391). Discovering the sections removes that failure mode
    rather than correcting one instance of it.
    """
    sections: dict[str, str] = {}
    for name in ElspethSettings.model_fields:
        annotation = ElspethSettings.model_fields[name].annotation
        origin = get_origin(annotation)
        args = get_args(annotation)
        if origin is dict and len(args) == 2:
            element, shape = args[1], "dict"
        elif origin is list and args:
            element, shape = args[0], "list"
        else:
            continue
        if not (isinstance(element, type) and issubclass(element, BaseModel)):
            continue
        if "plugin" in element.model_fields and "options" in element.model_fields:
            sections[name] = shape
    return sections


def _declared_emitted_options(plugin_name: str) -> dict[str, list[tuple[str, str]]]:
    """Return ``{option_name: [(plugin_kind, reason), ...]}`` for ``plugin_name``.

    Literal values that reach output come from ``EmittedToOutput`` metadata on
    plugin config fields. Transform options whose value names a field the
    transform writes come from ``BaseTransform.output_naming_config_keys`` —
    the existing authority whose declarations are checked against actual
    created fields by the transform invariant suite. Those names are output
    too: env expansion can turn ``${VAR}`` into a valid field name, after which
    plugin validation accepts the host value and the transform writes it as a
    row key and downstream artifact header. The union avoids a second
    hand-maintained copy of every output-field option in config annotations.

    Unioned across all three registries, deliberately, because there is no
    name-to-kind lookup: the registries are three disjoint maps, and ``csv``,
    ``json``, ``aws_s3``, ``azure_blob``, ``dataverse``, ``text`` and ``llm``
    each name a plugin in more than one of them.

    Classifying the section instead — "``sinks`` means look in the sink
    registry" — would reintroduce a hand-maintained fact whose failure mode is
    a SILENT FAIL-OPEN: a section mapped to the wrong kind reads the wrong
    declarations, forbids nothing, and reports success. Unioning fails the
    other way. A dual-kind name may import a sibling's restriction and refuse a
    placeholder that would have been harmless, which is loud, happens at config
    load, and is fixed in one line. For a security control those two residuals
    are directions, not a trade-off.

    The lookup is by membership rather than by catching a not-found error, so a
    missing plugin is an ordinary absence here. An unregistered name is left
    alone: naming a plugin that does not exist is a different error, reported
    later by the plugin factory with better context than this guard could give.
    """
    from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager

    manager = get_shared_plugin_manager()
    registries: tuple[tuple[str, list[Any]], ...] = (
        ("source", list(manager.get_sources())),
        ("transform", list(manager.get_transforms())),
        ("sink", list(manager.get_sinks())),
    )

    declared: dict[str, list[tuple[str, str]]] = {}
    for kind, plugin_classes in registries:
        for plugin_class in plugin_classes:
            if plugin_class.name != plugin_name:
                continue
            explicitly_emitted = emitted_option_fields(plugin_class.get_config_model())
            for option_name, reason in explicitly_emitted.items():
                declared.setdefault(option_name, []).append((kind, reason))
            if kind == "transform":
                for option_name in plugin_class.output_naming_config_keys:
                    if option_name in explicitly_emitted:
                        continue
                    declared.setdefault(option_name, []).append(
                        (
                            kind,
                            "this option names a field the transform writes, so its value becomes "
                            "a key in row data and a column in downstream artifacts",
                        )
                    )
    return declared


@trust_boundary(
    tier=3,
    source=(
        "raw pipeline configuration mapping — operator YAML via Dynaconf or a web-authored dict — "
        "before env expansion and ElspethSettings validation"
    ),
    source_param="raw_config",
    suppresses=("R5",),
    invariant=(
        "raises ValueError when any plugin-bearing entry holds an environment-variable placeholder in an "
        "option its plugin declares output-reaching through EmittedToOutput or output_naming_config_keys; "
        "entries whose plugin name or options are not the expected scalar/mapping shape are skipped here "
        "and rejected by ElspethSettings validation"
    ),
    test_ref="tests/unit/core/test_config.py::TestEnvPlaceholderGuardIsDerived::test_rejection_message_names_the_declaring_kind",
    test_fingerprint="5423e68f4c1534e2c8c66c5e3a396e890593343f71449e5e436d65616b4649c4",
)
def _reject_sensitive_plugin_env_placeholders_before_expansion(raw_config: Mapping[str, object]) -> None:
    """Reject env placeholders in plugin options whose value is written to output.

    Some plugin options are not references or selectors — their literal value is
    rendered into row data or into an artifact's bytes. Reject their raw
    ``${VAR}`` before the loader expands it, so the plugin's own validation
    cannot be bypassed by handing it an already-expanded host value.

    DERIVED, NOT RESTATED. Both the sections and the forbidden fields come from
    the system's own declarations: the sections from ``ElspethSettings``;
    literal output values from
    :class:`~elspeth.contracts.emitted_option.EmittedToOutput` markers on each
    plugin's config model; and transform output-field names from the
    truth-tested ``BaseTransform.output_naming_config_keys`` authority. This
    function names no plugin and no option.

    It replaced a hand-maintained ``{plugin: {field, ...}}`` map that held ONE
    plugin and two fields, and was therefore a no-op for every other plugin.
    ``truncate.suffix`` and ``csv.headers`` both reached artifact bytes through
    the gap (elspeth-8f0a6b3391), and ``collectors`` was not walked at all
    (elspeth-bc1b2c2959). The two artifacts agreed only by luck: one declarer,
    one map entry.

    This guard is NOT redundant with the plugin-side validator. On the CLI/YAML
    path ``_expand_env_vars`` runs first, so the plugin validator sees a clean
    expanded string that no longer matches ``${...}``. Nor is it redundant with
    ``_fingerprint_config_for_audit``, which matches on the option NAME and so
    never trips on a presentation field like ``title``.

    Raises:
        ValueError: An option declared output-reaching through
            ``EmittedToOutput`` or ``output_naming_config_keys`` holds an env
            placeholder. The message names the declaring plugin kind, because
            a union across registries can refuse a source option on a sink's
            declaration and an unexplained refusal is worse than the refusal.
    """
    for section_name, shape in sorted(_plugin_bearing_sections().items()):
        section = raw_config[section_name] if section_name in raw_config else None
        if shape == "dict":
            entries: list[tuple[object, object]] = list(section.items()) if type(section) is dict else []
        else:
            entries = list(enumerate(section)) if type(section) is list else []

        for entry_key, entry in entries:
            if type(entry) is not dict:
                continue
            plugin_name = entry["plugin"] if "plugin" in entry else None
            options = entry["options"] if "options" in entry else None
            if not isinstance(plugin_name, str) or type(options) is not dict:
                continue

            declarations = _declared_emitted_options(plugin_name)
            for option_name in sorted(declarations):
                if option_name not in options or not env_placeholders_in(options[option_name]):
                    continue
                sources = declarations[option_name]
                reason = "; ".join(f"as a {kind} plugin, {why}" for kind, why in sources)
                shared = ""
                if len({kind for kind, _ in sources}) > 1:
                    shared = (
                        f" The name {plugin_name!r} is registered as more than one plugin kind "
                        f"({', '.join(sorted(kind for kind, _ in sources))}) and this option is declared "
                        f"emitted by more than one of them, so the restriction applies here regardless of "
                        f"which kind this section resolves to."
                    )
                raise ValueError(
                    f"{section_name}[{entry_key!r}] {plugin_name} option {option_name!r} "
                    f"must not contain environment-variable placeholders before env expansion: "
                    f"{reason}.{shared} Env expansion runs before plugin validation on this path, so the "
                    f"placeholder would be replaced by the host value and written out verbatim."
                )


def load_settings(config_path: Path) -> ElspethSettings:
    """Load settings from YAML file with environment variable overrides.

    Uses Dynaconf for multi-source loading with precedence:
    1. Environment variables (ELSPETH_*) - highest priority
    2. Config file (settings.yaml)
    3. Defaults from Pydantic schema - lowest priority

    Environment variable format: ELSPETH_DATABASE__URL for nested keys.

    Args:
        config_path: Path to YAML configuration file

    Returns:
        Validated ElspethSettings instance

    Raises:
        ValidationError: If configuration fails Pydantic validation
        FileNotFoundError: If config file doesn't exist
    """
    from dynaconf import Dynaconf

    # Explicit check for file existence (Dynaconf silently accepts missing files)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    # Load from file + environment
    dynaconf_settings = Dynaconf(
        envvar_prefix="ELSPETH",
        settings_files=[str(config_path)],
        environments=False,  # No [default]/[production] sections
        load_dotenv=False,  # Don't auto-load .env
        merge_enabled=True,  # Deep merge nested dicts
    )

    # Dynaconf returns uppercase keys; convert to lowercase for Pydantic
    raw_dict = dynaconf_settings.as_dict()
    raw_config = _lowercase_schema_keys(raw_dict)

    # Reject unknown YAML keys before filtering. Only check keys that originate
    # from the YAML file, NOT from environment variables. Dynaconf captures ALL
    # ELSPETH_* env vars (e.g., ELSPETH_LOG_LEVEL → "log_level") and injects
    # them into raw_config. These are legitimate runtime env vars, not typos.
    known_fields = set(ElspethSettings.model_fields.keys())
    with open(config_path) as _f:
        _yaml_loaded = yaml.safe_load(_f)
    # ONLY an empty document (None) means "no keys". Every other falsy document
    # (false, 0, "", []) is a non-mapping that must be REJECTED below — the old
    # `or {}` coerced those into a valid-looking empty mapping (fail-open).
    _yaml_only = {} if _yaml_loaded is None else _yaml_loaded
    if not isinstance(_yaml_only, dict):
        raise ValueError(f"Configuration file {config_path.name} must be a YAML mapping (key: value), not {type(_yaml_only).__name__}")
    yaml_keys_lower = {str(k).lower() for k in _yaml_only}
    unknown_yaml_keys = sorted(k for k in yaml_keys_lower if k not in known_fields and k not in _DYNACONF_INTERNAL_KEYS)
    if unknown_yaml_keys:
        raise ValueError(
            f"Unknown configuration keys in {config_path.name}: {unknown_yaml_keys}. Check for typos. Valid top-level keys: {sorted(known_fields)}"
        )

    # Filter Dynaconf internals (now safe — all non-known keys are Dynaconf's)
    raw_config = {k: v for k, v in raw_config.items() if k in known_fields}
    _reject_sensitive_plugin_env_placeholders_before_expansion(raw_config)

    # Lower `llm` transform nodes that select an operator profile alias into
    # their private executable provider config, mirroring the web
    # plugin-policy lowering seam (see _lower_llm_profile_nodes docstring).
    raw_config = _lower_llm_profile_nodes(raw_config, materialize=True)

    # Expand ${VAR} and ${VAR:-default} patterns in config values
    raw_config = _expand_env_vars(raw_config)

    # Expand template files in plugin options before validation
    # NOTE: Secrets are NOT fingerprinted here - they stay available for runtime.
    # Fingerprinting happens in resolve_config() when creating the audit copy.
    raw_config = _expand_config_templates(raw_config, settings_path=config_path)

    return ElspethSettings(**raw_config)


def load_settings_from_config_dict(config_dict: Mapping[str, object], *, expand_env_vars: bool = False) -> ElspethSettings:
    """Load settings from an already parsed in-memory config dict.

    This is the common post-parse path for web execution and validation.
    It skips Dynaconf (no env var merging) and file I/O, ensuring resolved
    secrets and inline blob contents never need to be serialized back to
    YAML before validation. File-backed template options (template_file,
    lookup_file, system_prompt_file) are rejected because there is no
    trusted settings-file root for resolving them.

    Args:
        config_dict: Parsed YAML configuration mapping.
        expand_env_vars: Whether to expand ``${VAR}`` and ``${VAR:-default}``
            patterns from the host environment. Defaults to ``False`` because
            this in-memory loader is used for web-authored YAML, which is
            user-controlled. Known secret inventory names are resolved via the
            audited resolve_secret_refs() path beforehand, and any remaining
            ``${VAR}`` must stay literal data rather than become a
            host-environment lookup. Trusted in-process callers that intentionally
            want host environment expansion must opt in explicitly.

    Returns:
        Validated ElspethSettings instance.
    """
    raw_config = _lowercase_schema_keys(dict(config_dict))
    known_fields = set(ElspethSettings.model_fields.keys())

    unknown_keys = sorted(k for k in raw_config if k not in known_fields)
    if unknown_keys:
        raise ValueError(f"Unknown configuration keys: {unknown_keys}. Valid top-level keys: {sorted(known_fields)}")

    raw_config = {k: v for k, v in raw_config.items() if k in known_fields}
    _reject_file_backed_template_options_for_in_memory_loader(raw_config)
    _reject_sensitive_plugin_env_placeholders_before_expansion(raw_config)
    # Structural profile-selector checks (unknown alias, ambiguous
    # profile+provider) always run; the credential-materializing rewrite only
    # runs when the caller opted into host environment expansion (see
    # _lower_llm_profile_nodes docstring) — otherwise the ${VAR} template it
    # writes would never be resolved and would reach plugin construction
    # as literal, unexpanded text instead of failing closed at load time.
    raw_config = _lower_llm_profile_nodes(raw_config, materialize=expand_env_vars)
    if expand_env_vars:
        raw_config = _expand_env_vars(raw_config)
    return ElspethSettings(**raw_config)


@trust_boundary(
    tier=3,
    source=("a web-authored pipeline YAML string — user-controlled content the web execution service loads without touching disk"),
    source_param="yaml_content",
    suppresses=("R5",),
    invariant=(
        "raises ValueError when the bounded YAML load yields anything but a mapping (and, downstream, "
        "when ElspethSettings validation rejects the content); never coerces a non-mapping document"
    ),
    test_ref="tests/unit/core/test_config.py::TestLoadSettingsFromYamlStringBoundary::test_non_mapping_yaml_document_raises",
    test_fingerprint="c7191a8e2aa172c5b106dc0133d7999b2b56178482b5e6a695b88b20a0e3ff8b",
)
def load_settings_from_yaml_string(yaml_content: str, *, expand_env_vars: bool = False) -> ElspethSettings:
    """Load settings from a YAML string without touching disk.

    This is used by the web execution service to load pipeline configs
    that may contain resolved secrets. Unlike load_settings(), this
    skips Dynaconf (no env var merging) and file I/O, ensuring secret
    values never leave process memory. File-backed template options
    (template_file, lookup_file, system_prompt_file) are rejected because
    there is no trusted settings-file root for resolving them.

    Args:
        yaml_content: YAML configuration as a string.
        expand_env_vars: Whether to expand ``${VAR}`` and ``${VAR:-default}``
            patterns from the host environment. Defaults to ``False`` because
            this in-memory loader is used for web-authored YAML, which is
            user-controlled. Known secret inventory names are resolved via the
            audited resolve_secret_refs() path beforehand, and any remaining
            ``${VAR}`` must stay literal data rather than become a
            host-environment lookup. Trusted in-process callers that intentionally
            want host environment expansion must opt in explicitly.

    Returns:
        Validated ElspethSettings instance.
    """
    config_dict = load_bounded_pipeline_yaml(yaml_content)
    if not isinstance(config_dict, dict):
        raise ValueError(f"Configuration must be a YAML mapping (key: value), not {type(config_dict).__name__}")
    return load_settings_from_config_dict(config_dict, expand_env_vars=expand_env_vars)
