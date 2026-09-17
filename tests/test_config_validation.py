"""AEGIS Declarative Configuration Subsystem - Deterministic Unit Tests.

Prompt: 006
Scope: Validates typed configuration models, deterministic loading, precedence ordering,
strict validation, environment overrides, and zero-secret heuristics.
Zero network access, zero model loading, zero database dependencies.
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from src.config.loader import ConfigurationError, load_config
from src.config.models import (
    AegisConfig,
    AutonomyTier,
    HardwareProfile,
    HardwareProfileState,
    NetworkMode,
    PathConfig,
    SystemConfig,
)


@pytest.fixture
def example_yaml_path() -> Path:
    """Path to canonical example YAML configuration."""
    return Path(__file__).resolve().parent.parent / "config" / "aegis.example.yaml"


@pytest.fixture
def temp_yaml(tmp_path: Path):
    """Helper to write temporary YAML files."""

    def _create(content: str, filename: str = "test_aegis.yaml") -> Path:
        p = tmp_path / filename
        p.write_text(content, encoding="utf-8")
        return p

    return _create


def test_01_default_configuration_loads():
    """Verify that empty/default configuration initializes with valid defaults."""
    cfg = load_config(apply_env_overrides=False)
    assert isinstance(cfg, AegisConfig)
    assert cfg.system.config_version == "1.0.0"
    assert cfg.system.environment == "development"
    assert cfg.autonomy.autonomy_tier == AutonomyTier.TIER_1_SAFE_BOUNDED
    assert cfg.network.network_mode == NetworkMode.BALANCED


def test_02_valid_yaml_loads(example_yaml_path: Path):
    """Verify loading canonical example YAML configuration file."""
    assert example_yaml_path.is_file(), f"Example YAML not found at {example_yaml_path}"
    cfg = load_config(config_file=example_yaml_path, apply_env_overrides=False)
    assert cfg.system.environment == "development"
    assert cfg.storage.max_storage_allocation_bytes == 50 * 1024 * 1024 * 1024
    assert cfg.channels.desktop_enabled is True
    assert cfg.channels.whatsapp_enabled is False


def test_03_invalid_yaml_fails_cleanly(temp_yaml):
    """Verify syntax error in YAML raises ConfigurationError cleanly."""
    bad_yaml = temp_yaml("system:\n  config_version: [unclosed_list")
    with pytest.raises(ConfigurationError) as exc_info:
        load_config(config_file=bad_yaml, apply_env_overrides=False)
    assert "YAML syntax error" in str(exc_info.value)


def test_04_wrong_field_type_fails(temp_yaml):
    """Verify type mismatch in configuration fields raises structured ConfigurationError."""
    bad_yaml = temp_yaml("storage:\n  max_storage_allocation_bytes: 'not_an_integer'")
    with pytest.raises(ConfigurationError) as exc_info:
        load_config(config_file=bad_yaml, apply_env_overrides=False)
    assert "Configuration validation failed" in str(exc_info.value)
    assert "max_storage_allocation_bytes" in str(exc_info.value)


def test_05_unknown_field_fails(temp_yaml):
    """Verify unknown/misspelled field is rejected via extra='forbid'."""
    bad_yaml = temp_yaml("system:\n  unknown_setting_misspelled: true")
    with pytest.raises(ConfigurationError) as exc_info:
        load_config(config_file=bad_yaml, apply_env_overrides=False)
    assert "unknown_setting_misspelled" in str(exc_info.value)
    assert "Extra inputs are not permitted" in str(exc_info.value)


def test_06_environment_variable_override_works(monkeypatch):
    """Verify AEGIS_* environment variables override default and YAML settings."""
    monkeypatch.setenv("AEGIS_ENVIRONMENT", "production")
    monkeypatch.setenv("AEGIS_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("AEGIS_NETWORK_MODE", "LOCAL_ONLY")
    monkeypatch.setenv("AEGIS_AUTONOMY_TIER", "0")

    cfg = load_config(apply_env_overrides=True)
    assert cfg.system.environment == "production"
    assert cfg.logging.log_level == "DEBUG"
    assert cfg.network.network_mode == NetworkMode.LOCAL_ONLY
    assert cfg.autonomy.autonomy_tier == AutonomyTier.TIER_0_STRICT_MANUAL


def test_07_precedence_is_deterministic(temp_yaml, monkeypatch):
    """Verify strict precedence: defaults < YAML < env var < runtime overrides."""
    # 1. Base YAML sets log_level to WARNING
    yaml_file = temp_yaml("logging:\n  log_level: WARNING")

    # 2. Env var overrides YAML with ERROR
    monkeypatch.setenv("AEGIS_LOG_LEVEL", "ERROR")

    # 3. Runtime override overrides env var with CRITICAL
    runtime = {"logging": {"log_level": "CRITICAL"}}

    cfg = load_config(config_file=yaml_file, runtime_overrides=runtime, apply_env_overrides=True)
    assert cfg.logging.log_level == "CRITICAL"


def test_08_default_values_are_applied(temp_yaml):
    """Verify partial YAML merges with default values without discarding unspecified fields."""
    partial_yaml = temp_yaml("notifications:\n  quiet_hours_enabled: true")
    cfg = load_config(config_file=partial_yaml, apply_env_overrides=False)
    # Modified field
    assert cfg.notifications.quiet_hours_enabled is True
    # Unmodified sibling default fields preserved
    assert cfg.notifications.default_priority == "P2"
    assert cfg.notifications.batch_interval_seconds == 60
    assert cfg.system.config_version == "1.0.0"


def test_09_path_handling_works():
    """Verify PathConfig resolves relative paths safely without hardcoded machine paths."""
    paths = PathConfig(repository_root="C:/MockProject/Root")
    resolved_data = paths.resolve_path("data/test.db")
    assert resolved_data == Path("C:/MockProject/Root/data/test.db")

    # Absolute path remains unchanged
    resolved_abs = paths.resolve_path("D:/Absolute/Path/file.txt")
    assert resolved_abs == Path("D:/Absolute/Path/file.txt")


def test_10_autonomy_tiers_validate():
    """Verify autonomy tiers enforce boundaries 0 to 3."""
    # Valid tiers
    for tier_val in [0, 1, 2, 3]:
        cfg = AegisConfig(autonomy={"autonomy_tier": tier_val})
        assert cfg.autonomy.autonomy_tier == tier_val

    # Invalid tier values
    for invalid in [-1, 4, 99]:
        with pytest.raises(ValidationError):
            AegisConfig(autonomy={"autonomy_tier": invalid})


def test_11_network_modes_validate():
    """Verify network mode enum validates exact allowed strings."""
    for mode in ["LOCAL_ONLY", "PREFER_LOCAL", "BALANCED", "ONLINE_ALLOWED"]:
        cfg = AegisConfig(network={"network_mode": mode})
        assert cfg.network.network_mode == mode

    with pytest.raises(ValidationError):
        AegisConfig(network={"network_mode": "UNRESTRICTED_INTERNET"})


def test_12_hardware_profile_validates():
    """Verify structured hardware profile data model instantiation."""
    hw = HardwareProfile(
        profile_id="test-hw-1",
        status=HardwareProfileState.VALID,
        cpu={"vendor": "Intel", "physical_cores": 10, "logical_processors": 16},
        gpu={"vendor": "NVIDIA", "vram_bytes": 6 * 1024 * 1024 * 1024},
    )
    assert hw.status == HardwareProfileState.VALID
    assert hw.cpu.physical_cores == 10
    assert hw.gpu.vram_bytes == 6 * 1024 * 1024 * 1024


def test_13_invalid_hardware_profile_state_fails():
    """Verify invalid hardware profile state is rejected."""
    with pytest.raises(ValidationError):
        HardwareProfile(status="UNKNOWN_INVALID_STATE")


def test_14_storage_limits_validate():
    """Verify storage boundary validation (minimum limits and percentage thresholds)."""
    # Low disk threshold must be at least 500MB
    with pytest.raises(ValidationError):
        AegisConfig(storage={"low_disk_threshold_bytes": 1024})

    # Cleanup threshold must be between 50.0 and 99.0
    with pytest.raises(ValidationError):
        AegisConfig(storage={"cleanup_threshold_percent": 105.0})


def test_15_secrets_are_rejected_from_ordinary_config(temp_yaml):
    """Verify heuristic rejection of secret keys and secret patterns in config."""
    # Forbidden key name 'api_key'
    bad_key_yaml = temp_yaml("system:\n  api_key: 'some_secret_key'")
    with pytest.raises(ConfigurationError) as exc_info:
        load_config(config_file=bad_key_yaml, apply_env_overrides=False)
    assert "sensitive credentials" in str(exc_info.value).lower()

    # Secret pattern matching OpenAI key format
    bad_val_yaml = temp_yaml("system:\n  installation_id: 'sk-abcdef1234567890abcdef1234567890'")
    with pytest.raises(ConfigurationError) as exc_info:
        load_config(config_file=bad_val_yaml, apply_env_overrides=False)
    assert "known secret format" in str(exc_info.value).lower()


def test_16_configuration_version_validates():
    """Verify config_version follows semver pattern."""
    assert SystemConfig(config_version="1.0").config_version == "1.0"
    assert SystemConfig(config_version="2.1.4").config_version == "2.1.4"

    with pytest.raises(ValidationError):
        SystemConfig(config_version="alpha_release")


def test_17_effective_configuration_is_reproducible(example_yaml_path: Path):
    """Verify two independent load cycles with identical inputs produce identical dumps."""
    cfg1 = load_config(config_file=example_yaml_path, apply_env_overrides=False)
    cfg2 = load_config(config_file=example_yaml_path, apply_env_overrides=False)
    assert cfg1.model_dump() == cfg2.model_dump()
