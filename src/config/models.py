"""AEGIS Declarative Configuration Subsystem - Domain Models.

Document Reference: docs/specifications/AEGIS_CONFIGURATION_SUBSYSTEM.md
Constraints:
- Configuration is NOT security authority (Trust Core remains authority).
- Typed Pydantic v2 schemas with extra='forbid' to reject unknown keys.
- Zero secrets permitted in ordinary configuration fields.
- Deterministic defaults and serialization.
"""

import re
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ==============================================================================
# ENUMERATIONS
# ==============================================================================


class HardwareProfileState(str, Enum):
    """Operational lifecycle state of a hardware benchmark profile."""

    VALID = "VALID"
    STALE = "STALE"
    INVALID = "INVALID"
    REQUIRES_REBENCHMARK = "REQUIRES_REBENCHMARK"


class AutonomyTier(int, Enum):
    """Frozen operational autonomy tiers (0 to 3)."""

    TIER_0_STRICT_MANUAL = 0
    TIER_1_SAFE_BOUNDED = 1
    TIER_2_SUPERVISED = 2
    TIER_3_FULL_BOUNDED = 3


class NetworkMode(str, Enum):
    """Operational network policy boundary."""

    LOCAL_ONLY = "LOCAL_ONLY"
    PREFER_LOCAL = "PREFER_LOCAL"
    BALANCED = "BALANCED"
    ONLINE_ALLOWED = "ONLINE_ALLOWED"


class LogLevel(str, Enum):
    """Logging verbosity levels."""

    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class SQLiteJournalMode(str, Enum):
    """Supported SQLite journal modes."""

    WAL = "WAL"
    MEMORY = "MEMORY"


class SQLiteSynchronousMode(str, Enum):
    """Supported SQLite synchronous modes."""

    FULL = "FULL"
    NORMAL = "NORMAL"
    OFF = "OFF"


class SQLiteTempStoreMode(str, Enum):
    """Supported SQLite temp_store modes."""

    MEMORY = "MEMORY"
    FILE = "FILE"
    DEFAULT = "DEFAULT"


# ==============================================================================
# BASE CONFIGURATION MODEL
# ==============================================================================


class AegisBaseConfig(BaseModel):
    """Base model enforcing strict key validation and zero-secret heuristics."""

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        use_enum_values=True,
    )

    @model_validator(mode="before")
    @classmethod
    def check_for_secrets(cls, data: Any) -> Any:
        """Scan input dictionary to reject raw credentials or secret patterns."""
        if not isinstance(data, dict):
            return data

        secret_patterns = [
            re.compile(r"^sk-[A-Za-z0-9_-]{20,}$"),
            re.compile(r"^ghp_[A-Za-z0-9]{30,}$"),
            re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
        ]

        for k, v in data.items():
            key_lower = str(k).lower()
            key_tokens = set(key_lower.replace("-", "_").split("_"))
            # Match exact sensitive credential terms, avoiding false positives on 'tokens'
            sensitive_terms = {"password", "secret", "api_key", "private_key", "credential"}
            if (
                key_lower in sensitive_terms
                or any(term in key_tokens for term in sensitive_terms)
                or (key_lower == "token" or key_tokens == {"token"})
            ):
                raise ValueError(
                    f"Configuration field '{k}' appears to contain sensitive credentials. "
                    f"Raw secrets are strictly prohibited from ordinary configuration."
                )

            # If string value matches known high-entropy secret patterns
            if isinstance(v, str):
                for pat in secret_patterns:
                    if pat.search(v):
                        raise ValueError(
                            f"Field '{k}' contains a value matching a known secret format. "
                            f"Credentials must be managed via Credential Broker."
                        )
        return data


# ==============================================================================
# 1. SYSTEM CONFIGURATION
# ==============================================================================


class SystemConfig(AegisBaseConfig):
    """System-level runtime metadata and versioning."""

    config_version: str = Field(default="1.0.0", description="Configuration schema version.")
    installation_id: Optional[str] = Field(
        default=None, description="Unique installation GUID or reference."
    )
    environment: str = Field(
        default="development",
        description="Operating environment name ('development', 'testing', 'production').",
    )
    platform: str = Field(default="windows", description="Target operating system platform.")
    diagnostics_mode: bool = Field(
        default=False, description="Enable continuous system health monitoring."
    )
    dev_mode: bool = Field(
        default=True, description="Enables developer diagnostics (does not weaken Trust Core)."
    )

    @field_validator("config_version")
    @classmethod
    def validate_config_version(cls, v: str) -> str:
        if not re.match(r"^\d+\.\d+(\.\d+)?$", v):
            raise ValueError(
                f"Invalid config_version format: '{v}'. Expected semver (e.g. '1.0.0')."
            )
        return v


# ==============================================================================
# 2. PATHS CONFIGURATION
# ==============================================================================


class PathConfig(AegisBaseConfig):
    """Logical paths utilized by AEGIS, resolved dynamically without hard-coded paths."""

    repository_root: str = Field(default=".", description="Base canonical repository root path.")
    data_dir: str = Field(
        default="data", description="Directory for local database and persistent storage."
    )
    runtime_dir: str = Field(
        default="runtime", description="Directory for runtime state and model staging."
    )
    cache_dir: str = Field(
        default="runtime/cache", description="Directory for ephemeral derived caches."
    )
    logs_dir: str = Field(
        default="runtime/logs", description="Directory for diagnostic and operation logs."
    )
    diagnostics_dir: str = Field(
        default="diagnostics", description="Directory for health probes and diagnostic dumps."
    )
    temp_dir: str = Field(
        default="runtime/scratch", description="Directory for tool subprocess scratchpads."
    )
    backup_dir: str = Field(
        default="data/backups", description="Directory for local recovery backup sets."
    )
    workspace_dir: str = Field(
        default="workspace", description="Directory for active task file manipulation."
    )

    def resolve_path(self, target: str | Path) -> Path:
        """Resolve a relative or absolute path against repository_root."""
        p = Path(target)
        if p.is_absolute():
            return p
        return (Path(self.repository_root) / p).resolve()


# ==============================================================================
# 3. STORAGE & RESOURCE POLICY CONFIGURATION
# ==============================================================================


class StoragePolicyConfig(AegisBaseConfig):
    """Storage limits and disk hygiene thresholds."""

    max_storage_allocation_bytes: int = Field(
        default=50 * 1024 * 1024 * 1024,  # 50 GB
        ge=1024 * 1024 * 1024,
        description="Maximum storage allocated to AEGIS across all domains.",
    )
    cache_limit_bytes: int = Field(
        default=5 * 1024 * 1024 * 1024,  # 5 GB
        ge=100 * 1024 * 1024,
        description="Maximum ephemeral cache size before LRU eviction.",
    )
    backup_limit_bytes: int = Field(
        default=20 * 1024 * 1024 * 1024,  # 20 GB
        ge=1024 * 1024 * 1024,
        description="Maximum total storage for local recovery backups.",
    )
    model_storage_allocation_bytes: int = Field(
        default=20 * 1024 * 1024 * 1024,  # 20 GB
        ge=1024 * 1024 * 1024,
        description="Maximum storage for local model checkpoints.",
    )
    user_data_allocation_bytes: int = Field(
        default=20 * 1024 * 1024 * 1024,  # 20 GB
        ge=1024 * 1024 * 1024,
        description="Maximum storage for user artifacts and databases.",
    )
    temp_storage_limit_bytes: int = Field(
        default=2 * 1024 * 1024 * 1024,  # 2 GB
        ge=50 * 1024 * 1024,
        description="Maximum temporary execution buffer allocation.",
    )
    low_disk_threshold_bytes: int = Field(
        default=5 * 1024 * 1024 * 1024,  # 5 GB
        ge=500 * 1024 * 1024,
        description="Threshold triggering low-disk warning and throttle.",
    )
    cleanup_threshold_percent: float = Field(
        default=85.0,
        ge=50.0,
        le=99.0,
        description="Storage utilization percentage triggering automated cache cleanup.",
    )
    # SQLite Persistence Runtime Configuration (Configuration policy, not security authority)
    database_path: Optional[str] = Field(
        default=None,
        description="Explicit database path. If None, resolved outside OneDrive.",
    )
    busy_timeout_ms: int = Field(
        default=5000,
        ge=100,
        le=60000,
        description="SQLite busy timeout in milliseconds (100ms - 60000ms).",
    )
    journal_mode: SQLiteJournalMode = Field(
        default=SQLiteJournalMode.WAL,
        description="SQLite journal mode constraint (WAL or MEMORY).",
    )
    synchronous: SQLiteSynchronousMode = Field(
        default=SQLiteSynchronousMode.NORMAL,
        description="SQLite synchronous pragma constraint (FULL, NORMAL, or OFF).",
    )
    foreign_keys: bool = Field(
        default=True,
        description="Enforce SQLite relational foreign key integrity.",
    )
    temp_store: SQLiteTempStoreMode = Field(
        default=SQLiteTempStoreMode.MEMORY,
        description="SQLite temp store location constraint (MEMORY, FILE, or DEFAULT).",
    )


# ==============================================================================
# 4. HARDWARE PROFILE STRUCTURES
# ==============================================================================


class CPUProfile(AegisBaseConfig):
    """Detected CPU characteristics."""

    vendor: str = Field(default="Unknown")
    model: str = Field(default="Unknown")
    physical_cores: int = Field(default=1, ge=1)
    logical_processors: int = Field(default=1, ge=1)
    base_clock_mhz: Optional[int] = Field(default=None, ge=100)
    architecture: str = Field(default="x86_64")


class RAMProfile(AegisBaseConfig):
    """Detected RAM capacity."""

    total_bytes: int = Field(default=16 * 1024 * 1024 * 1024, ge=1024 * 1024 * 1024)
    available_bytes: Optional[int] = Field(default=None)
    memory_compression_active: bool = Field(default=False)


class GPUProfile(AegisBaseConfig):
    """Detected GPU characteristics."""

    vendor: str = Field(default="NVIDIA")
    model: str = Field(default="Unknown")
    vram_bytes: int = Field(default=6 * 1024 * 1024 * 1024, ge=0)
    driver_version: str = Field(default="Unknown")
    cuda_compute_capability: Optional[str] = Field(default=None)


class StorageDriveProfile(AegisBaseConfig):
    """Storage volume characteristics."""

    device_id: str = Field(default="C:")
    total_bytes: int = Field(default=500 * 1024 * 1024 * 1024, ge=1024 * 1024 * 1024)
    free_bytes: int = Field(default=100 * 1024 * 1024 * 1024, ge=0)
    filesystem: str = Field(default="NTFS")


class OSProfile(AegisBaseConfig):
    """Operating system characteristics."""

    operating_system: str = Field(default="Windows")
    version: str = Field(default="11")
    build_number: str = Field(default="26220")
    architecture: str = Field(default="64bit")


class RuntimeProfile(AegisBaseConfig):
    """Host execution runtimes."""

    python_version: str = Field(default="3.12.10")
    node_version: Optional[str] = Field(default="24.13.0")
    cuda_available: bool = Field(default=True)
    ollama_version: Optional[str] = Field(default="0.34.0")


class HardwareProfile(AegisBaseConfig):
    """Cached hardware profile data structure."""

    profile_id: str = Field(default="default-profile")
    status: HardwareProfileState = Field(default=HardwareProfileState.REQUIRES_REBENCHMARK)
    cpu: CPUProfile = Field(default_factory=CPUProfile)
    ram: RAMProfile = Field(default_factory=RAMProfile)
    gpu: GPUProfile = Field(default_factory=GPUProfile)
    storage: List[StorageDriveProfile] = Field(default_factory=lambda: [StorageDriveProfile()])
    os: OSProfile = Field(default_factory=OSProfile)
    runtimes: RuntimeProfile = Field(default_factory=RuntimeProfile)
    benchmark_version: str = Field(default="1.0")
    detection_version: str = Field(default="1.0")
    invalidation_reason: Optional[str] = Field(default=None)
    last_benchmarked_at: Optional[str] = Field(default=None)


# ==============================================================================
# 5. PROVISIONING POLICY CONFIGURATION
# ==============================================================================


class ProvisioningPolicyConfig(AegisBaseConfig):
    """Resource constraints for model and skill allocation."""

    max_model_storage_bytes: int = Field(
        default=25 * 1024 * 1024 * 1024,  # 25 GB
        ge=1024 * 1024 * 1024,
    )
    min_reserved_storage_bytes: int = Field(
        default=10 * 1024 * 1024 * 1024,  # 10 GB
        ge=1024 * 1024 * 1024,
    )
    preferred_residency: str = Field(
        default="manager_priority",
        description="VRAM residency strategy ('manager_priority', 'balanced', 'on_demand').",
    )
    resource_priority: str = Field(
        default="manager", description="Component with first claim on GPU acceleration."
    )
    vram_budget_bytes: int = Field(
        default=5 * 1024 * 1024 * 1024,  # 5 GB (out of 6 GB VRAM)
        ge=1024 * 1024 * 1024,
    )
    ram_budget_bytes: int = Field(
        default=4 * 1024 * 1024 * 1024,  # 4 GB system RAM limit for inference
        ge=512 * 1024 * 1024,
    )
    cpu_core_limit: int = Field(default=8, ge=1)
    optional_capability_budget: Dict[str, Any] = Field(default_factory=dict)


# ==============================================================================
# 6. AUTONOMY CONFIGURATION
# ==============================================================================


class AutonomyConfig(AegisBaseConfig):
    """Operational autonomy policy (decoupled from deterministic risk boundaries)."""

    autonomy_tier: AutonomyTier = Field(
        default=AutonomyTier.TIER_1_SAFE_BOUNDED,
        description=(
            "Operational autonomy level "
            "(0=Strict Manual, 1=Safe Bounded, 2=Supervised, 3=Full Bounded)."
        ),
    )
    allow_user_override: bool = Field(
        default=True,
        description="Whether user can temporarily elevate tier for a specific task.",
    )


# ==============================================================================
# 7. NETWORK MODE CONFIGURATION
# ==============================================================================


class NetworkConfig(AegisBaseConfig):
    """Network egress policy boundaries."""

    network_mode: NetworkMode = Field(
        default=NetworkMode.BALANCED,
        description=(
            "Operating network boundary "
            "('LOCAL_ONLY', 'PREFER_LOCAL', 'BALANCED', 'ONLINE_ALLOWED')."
        ),
    )
    request_timeout_seconds: int = Field(default=30, ge=5, le=300)
    max_retries: int = Field(default=3, ge=0, le=10)


# ==============================================================================
# 8. CHANNELS CONFIGURATION
# ==============================================================================


class ChannelConfig(AegisBaseConfig):
    """Channel interfaces and affinity rules."""

    desktop_enabled: bool = Field(default=True)
    whatsapp_enabled: bool = Field(default=False)
    channel_affinity_enforced: bool = Field(
        default=True, description="Ensures responses return exclusively to the originating channel."
    )
    allow_cross_channel_notifications: bool = Field(
        default=False,
        description="Cross-channel notifications permitted only through explicit user policy.",
    )
    allowed_cross_channel_priorities: List[str] = Field(
        default_factory=lambda: ["P0"],
        description="Priorities eligible for cross-channel routing when enabled.",
    )


# ==============================================================================
# 9. NOTIFICATIONS CONFIGURATION
# ==============================================================================


class NotificationConfig(AegisBaseConfig):
    """Alert routing, priority thresholds, and quiet hours."""

    quiet_hours_enabled: bool = Field(default=False)
    quiet_hours_start_utc: str = Field(default="22:00")
    quiet_hours_end_utc: str = Field(default="08:00")
    default_priority: str = Field(default="P2")
    batching_enabled: bool = Field(default=True)
    batch_interval_seconds: int = Field(default=60, ge=5, le=3600)
    deduplication_window_seconds: int = Field(default=300, ge=10, le=86400)


# ==============================================================================
# 10. MEMORY & CONTEXT CONFIGURATION
# ==============================================================================


class MemoryConfig(AegisBaseConfig):
    """Context budgets and memory retrieval limits."""

    max_working_context_tokens: int = Field(default=8192, ge=1024, le=128000)
    hot_memory_item_limit: int = Field(default=100, ge=10, le=1000)
    project_memory_limit_items: int = Field(default=5000, ge=100, le=100000)
    retrieval_candidate_limit: int = Field(default=50, ge=5, le=500)
    context_ir_token_budget: int = Field(default=4096, ge=512, le=32000)
    retention_policy_days: int = Field(default=365, ge=30, le=3650)


# ==============================================================================
# 11. LOGGING & DIAGNOSTICS CONFIGURATION
# ==============================================================================


class LoggingConfig(AegisBaseConfig):
    """Logging, telemetry, and diagnostics policies."""

    log_level: LogLevel = Field(default=LogLevel.INFO)
    diagnostic_mode: bool = Field(default=False)
    retention_days: int = Field(default=30, ge=1, le=365)
    audit_logging_enabled: bool = Field(default=True)
    performance_telemetry_enabled: bool = Field(default=False)


# ==============================================================================
# 12. DEVELOPER CONFIGURATION
# ==============================================================================


class DeveloperConfig(AegisBaseConfig):
    """Development and test overrides (strictly cannot bypass Trust Core)."""

    debug_mode: bool = Field(default=False)
    verbose_diagnostics: bool = Field(default=False)
    test_mode: bool = Field(default=False)


# ==============================================================================
# ROOT AEGIS CONFIGURATION CONTAINER
# ==============================================================================


class AegisConfig(AegisBaseConfig):
    """Root configuration container uniting all declarative AEGIS domains."""

    system: SystemConfig = Field(default_factory=SystemConfig)
    paths: PathConfig = Field(default_factory=PathConfig)
    storage: StoragePolicyConfig = Field(default_factory=StoragePolicyConfig)
    hardware: HardwareProfile = Field(default_factory=HardwareProfile)
    provisioning: ProvisioningPolicyConfig = Field(default_factory=ProvisioningPolicyConfig)
    autonomy: AutonomyConfig = Field(default_factory=AutonomyConfig)
    network: NetworkConfig = Field(default_factory=NetworkConfig)
    channels: ChannelConfig = Field(default_factory=ChannelConfig)
    notifications: NotificationConfig = Field(default_factory=NotificationConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    developer: DeveloperConfig = Field(default_factory=DeveloperConfig)
