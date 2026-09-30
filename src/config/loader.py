"""SAGE Declarative Configuration Subsystem - Deterministic Loader.

Document Reference: docs/specifications/AEGIS_CONFIGURATION_SUBSYSTEM.md
Implements:
1. Immutable application defaults
2. Static YAML configuration loading
3. Environment-specific YAML loading
4. Environment variable overrides (AEGIS_*)
5. Explicit runtime overrides
6. Strict validation and structured error reporting
"""

import os
from pathlib import Path
from typing import Any, Dict, Optional

import yaml
from pydantic import ValidationError

from .models import AegisConfig


class ConfigurationError(Exception):
    """Base exception for SAGE configuration loading and validation failures."""

    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.details = details or {}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively merge override dictionary into base dictionary."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _parse_env_var_value(val_str: str) -> Any:
    """Safely coerce primitive environment variable strings."""
    lower = val_str.lower()
    if lower in ("true", "1", "yes", "on"):
        return True
    if lower in ("false", "0", "no", "off"):
        return False
    # Try integer
    try:
        return int(val_str)
    except ValueError:
        pass
    # Try float
    try:
        return float(val_str)
    except ValueError:
        pass
    return val_str


def get_env_overrides() -> Dict[str, Any]:
    """Extract and map SAGE_* (canonical) and AEGIS_* (legacy) environment variables.

    Canonical SAGE mappings take precedence over legacy AEGIS mappings.
    Supported explicit mappings:
    - SAGE_ENVIRONMENT / AEGIS_ENVIRONMENT -> system.environment
    - SAGE_CONFIG_VERSION / AEGIS_CONFIG_VERSION -> system.config_version
    - SAGE_LOG_LEVEL / AEGIS_LOG_LEVEL -> logging.log_level
    - SAGE_NETWORK_MODE / AEGIS_NETWORK_MODE -> network.network_mode
    - SAGE_AUTONOMY_TIER / AEGIS_AUTONOMY_TIER -> autonomy.autonomy_tier
    - SAGE_DEBUG_MODE / AEGIS_DEBUG_MODE -> developer.debug_mode
    - SAGE_TEST_MODE / AEGIS_TEST_MODE -> developer.test_mode
    - SAGE_DESKTOP_ENABLED / AEGIS_DESKTOP_ENABLED -> channels.desktop_enabled
    - SAGE_WHATSAPP_ENABLED / AEGIS_WHATSAPP_ENABLED -> channels.whatsapp_enabled
    """
    overrides: Dict[str, Any] = {}

    # Legacy AEGIS mappings first (so SAGE_* overrides them if both exist)
    legacy_mapping = {
        "AEGIS_ENVIRONMENT": ("system", "environment"),
        "AEGIS_CONFIG_VERSION": ("system", "config_version"),
        "AEGIS_LOG_LEVEL": ("logging", "log_level"),
        "AEGIS_NETWORK_MODE": ("network", "network_mode"),
        "AEGIS_AUTONOMY_TIER": ("autonomy", "autonomy_tier"),
        "AEGIS_DEBUG_MODE": ("developer", "debug_mode"),
        "AEGIS_TEST_MODE": ("developer", "test_mode"),
        "AEGIS_DESKTOP_ENABLED": ("channels", "desktop_enabled"),
        "AEGIS_WHATSAPP_ENABLED": ("channels", "whatsapp_enabled"),
    }

    canonical_mapping = {
        "SAGE_ENVIRONMENT": ("system", "environment"),
        "SAGE_CONFIG_VERSION": ("system", "config_version"),
        "SAGE_LOG_LEVEL": ("logging", "log_level"),
        "SAGE_NETWORK_MODE": ("network", "network_mode"),
        "SAGE_AUTONOMY_TIER": ("autonomy", "autonomy_tier"),
        "SAGE_DEBUG_MODE": ("developer", "debug_mode"),
        "SAGE_TEST_MODE": ("developer", "test_mode"),
        "SAGE_DESKTOP_ENABLED": ("channels", "desktop_enabled"),
        "SAGE_WHATSAPP_ENABLED": ("channels", "whatsapp_enabled"),
    }

    for env_name, path in legacy_mapping.items():
        if env_name in os.environ:
            val = _parse_env_var_value(os.environ[env_name])
            domain, field = path
            if domain not in overrides:
                overrides[domain] = {}
            overrides[domain][field] = val

    for env_name, path in canonical_mapping.items():
        if env_name in os.environ:
            val = _parse_env_var_value(os.environ[env_name])
            domain, field = path
            if domain not in overrides:
                overrides[domain] = {}
            overrides[domain][field] = val

    return overrides


def load_yaml_file(file_path: Path) -> Dict[str, Any]:
    """Safely parse a YAML configuration file."""
    if not file_path.is_file():
        raise ConfigurationError(
            f"Configuration file not found: {file_path}", details={"file_path": str(file_path)}
        )
    try:
        content = file_path.read_text(encoding="utf-8")
        parsed = yaml.safe_load(content)
        if parsed is None:
            return {}
        if not isinstance(parsed, dict):
            raise ConfigurationError(
                f"Configuration file root must be a dict, got {type(parsed).__name__}",
                details={"file_path": str(file_path)},
            )
        return parsed
    except yaml.YAMLError as e:
        raise ConfigurationError(
            f"YAML syntax error in configuration file: {e}",
            details={"file_path": str(file_path), "error": str(e)},
        ) from e


def load_config(
    config_file: Optional[Path] = None,
    runtime_overrides: Optional[Dict[str, Any]] = None,
    apply_env_overrides: bool = True,
) -> AegisConfig:
    """Load, merge, and deterministically validate the effective AEGIS configuration.

    Precedence order:
    1. Immutable application defaults (AegisConfig defaults)
    2. Static configuration file (config_file)
    3. Environment-specific configuration file (e.g. aegis.<env>.yaml if present)
    4. Environment variables (AEGIS_*)
    5. Explicit runtime overrides (runtime_overrides)
    """
    # 1. Base default dictionary
    merged_data: Dict[str, Any] = {}

    # 2. Static configuration file
    if config_file is not None:
        file_data = load_yaml_file(config_file)
        merged_data = _deep_merge(merged_data, file_data)

        # 3. Check for environment-specific secondary file (e.g. config/aegis.development.yaml)
        env_name = merged_data.get("system", {}).get("environment")
        if env_name and isinstance(env_name, str):
            env_file = config_file.parent / f"{config_file.stem}.{env_name}{config_file.suffix}"
            if env_file.is_file():
                env_file_data = load_yaml_file(env_file)
                merged_data = _deep_merge(merged_data, env_file_data)

    # 4. Environment variables
    if apply_env_overrides:
        env_overrides = get_env_overrides()
        merged_data = _deep_merge(merged_data, env_overrides)

    # 5. Explicit runtime overrides
    if runtime_overrides:
        merged_data = _deep_merge(merged_data, runtime_overrides)

    # 6. Validate via Pydantic model
    try:
        return AegisConfig.model_validate(merged_data)
    except ValidationError as e:
        formatted_errors = []
        for err in e.errors():
            loc = " -> ".join(str(elem) for elem in err.get("loc", []))
            msg = err.get("msg", "Unknown validation error")
            formatted_errors.append(f"Field '{loc}': {msg}")

        summary = "\n  - ".join(formatted_errors)
        raise ConfigurationError(
            f"Configuration validation failed:\n  - {summary}",
            details={"validation_errors": e.errors()},
        ) from e
