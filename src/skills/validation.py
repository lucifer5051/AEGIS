"""SAGE Skills Subsystem -- Deterministic Manifest Validation.

Validates declarative SkillManifest instances fail-closed against schema,
naming, risk, capability, tool binding, and security invariants.
Operates 100% offline with zero network access.
"""

from __future__ import annotations

import re
from typing import Mapping, Optional, Sequence

from src.skills.errors import SkillManifestValidationError
from src.skills.models import (
    ExecutionStrategy,
    MemoryAccessPolicy,
    ProjectScope,
    SkillLifecycleState,
    SkillManifest,
    SkillRecoveryPolicy,
    SkillType,
    SkillVerificationPolicy,
)
from src.trust.capability import ActionRisk

# Canonical regex patterns
_SKILL_ID_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")
_TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(-[a-zA-Z0-9.]+)?$")


def validate_skill_manifest(
    manifest: SkillManifest,
    known_tools: Optional[Sequence[str]] = None,
    known_capabilities: Optional[Sequence[str]] = None,
) -> None:
    """Validate a SkillManifest deterministically and fail-closed.

    Args:
        manifest: The declarative SkillManifest to validate.
        known_tools: Optional sequence of known/registered tool names. If provided,
            manifest tool_bindings must be a subset of known tools.
        known_capabilities: Optional sequence of authorized system capabilities.

    Raises:
        SkillManifestValidationError: If any field or invariant is invalid.
    """
    if not isinstance(manifest, SkillManifest):
        raise SkillManifestValidationError(
            f"Expected SkillManifest instance, got {type(manifest).__name__}"
        )

    # 1. Skill ID validation
    if not manifest.skill_id or not manifest.skill_id.strip():
        raise SkillManifestValidationError("skill_id cannot be empty or whitespace.")
    if len(manifest.skill_id) > 128:
        raise SkillManifestValidationError(
            f"skill_id '{manifest.skill_id}' exceeds maximum length of 128 characters."
        )
    if not _SKILL_ID_RE.match(manifest.skill_id):
        raise SkillManifestValidationError(
            f"skill_id '{manifest.skill_id}' must match pattern "
            "'^[a-z][a-z0-9_]*(\\.[a-z][a-z0-9_]*)*$'."
        )

    # 2. Name & Description
    if not manifest.name or not manifest.name.strip():
        raise SkillManifestValidationError("Skill name cannot be empty.")
    if len(manifest.name) > 128:
        raise SkillManifestValidationError(
            f"Skill name '{manifest.name}' exceeds maximum length of 128 characters."
        )
    if not manifest.description or not manifest.description.strip():
        raise SkillManifestValidationError("Skill description cannot be empty.")

    # 3. SemVer validation
    if not manifest.version or not _SEMVER_RE.match(manifest.version):
        raise SkillManifestValidationError(
            f"Skill version '{manifest.version}' is not valid semantic versioning (e.g. '1.0.0')."
        )

    # 4. Enums and classifications
    if not isinstance(manifest.skill_type, SkillType):
        raise SkillManifestValidationError(
            f"Invalid skill_type: {manifest.skill_type}. Must be a SkillType enum value."
        )
    if not isinstance(manifest.execution_strategy, ExecutionStrategy):
        raise SkillManifestValidationError(
            f"Invalid execution_strategy: {manifest.execution_strategy}."
        )
    if not isinstance(manifest.risk_class, ActionRisk):
        raise SkillManifestValidationError(
            f"Invalid risk_class: {manifest.risk_class}. Must be an ActionRisk enum."
        )
    if not isinstance(manifest.verification_policy, SkillVerificationPolicy):
        raise SkillManifestValidationError(
            f"Invalid verification_policy: {manifest.verification_policy}."
        )
    if not isinstance(manifest.recovery_policy, SkillRecoveryPolicy):
        raise SkillManifestValidationError(f"Invalid recovery_policy: {manifest.recovery_policy}.")
    if not isinstance(manifest.memory_access, MemoryAccessPolicy):
        raise SkillManifestValidationError(
            f"Invalid memory_access policy: {manifest.memory_access}."
        )
    if not isinstance(manifest.project_scope, ProjectScope):
        raise SkillManifestValidationError(f"Invalid project_scope: {manifest.project_scope}.")
    if not isinstance(manifest.lifecycle_state, SkillLifecycleState):
        raise SkillManifestValidationError(f"Invalid lifecycle_state: {manifest.lifecycle_state}.")

    # 5. Schema validation
    if not isinstance(manifest.input_schema, (dict, Mapping)):
        raise SkillManifestValidationError("input_schema must be a dictionary.")
    if not isinstance(manifest.output_schema, (dict, Mapping)):
        raise SkillManifestValidationError("output_schema must be a dictionary.")

    # 6. Tool Bindings validation
    if not isinstance(manifest.tool_bindings, (tuple, list)):
        raise SkillManifestValidationError("tool_bindings must be a sequence of tool names.")
    for tool_name in manifest.tool_bindings:
        if not isinstance(tool_name, str) or not tool_name.strip():
            raise SkillManifestValidationError("Each tool binding must be a non-empty string.")
        if not _TOOL_NAME_RE.match(tool_name):
            raise SkillManifestValidationError(
                f"Tool binding '{tool_name}' does not match canonical tool naming pattern."
            )
        if known_tools is not None and tool_name not in known_tools:
            raise SkillManifestValidationError(
                f"Tool binding '{tool_name}' references an unknown tool not present in the catalog."
            )

    # 7. Required capabilities validation
    if not isinstance(manifest.required_capabilities, (tuple, list)):
        raise SkillManifestValidationError("required_capabilities must be a sequence of strings.")
    for cap_name in manifest.required_capabilities:
        if not isinstance(cap_name, str) or not cap_name.strip():
            raise SkillManifestValidationError(
                "Each required capability must be a non-empty string."
            )
        if known_capabilities is not None and cap_name not in known_capabilities:
            raise SkillManifestValidationError(
                f"Required capability '{cap_name}' is not recognized in the system "
                "capability registry."
            )

    # 8. Resource requirements validation
    res = manifest.resource_requirements
    if res.declared_marginal_ram_bytes < 0:
        raise SkillManifestValidationError("declared_marginal_ram_bytes cannot be negative.")
    if res.declared_marginal_vram_bytes < 0:
        raise SkillManifestValidationError("declared_marginal_vram_bytes cannot be negative.")
    if res.marginal_cpu_cores <= 0:
        raise SkillManifestValidationError("marginal_cpu_cores must be strictly positive.")
    if res.timeout_ms <= 0:
        raise SkillManifestValidationError("timeout_ms must be strictly positive.")

    # 9. Concurrency policy validation
    concurrency = manifest.concurrency_policy
    if concurrency.max_concurrent < 1:
        raise SkillManifestValidationError("max_concurrent must be at least 1.")

    # 10. Security Invariant: Skills cannot define Trust Core policies
    # Ensure no reserved or forbidden keys exist in schemas attempting to inject permissions
    for schema_dict in (manifest.input_schema, manifest.output_schema):
        forbidden_keys = {
            "trust_core_override",
            "grant_capability",
            "bypass_policy",
            "escalate_tier",
        }
        if any(k in schema_dict for k in forbidden_keys):
            raise SkillManifestValidationError(
                "Skill manifest cannot declare Trust Core policy modification keys."
            )
