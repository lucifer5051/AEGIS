"""Canonical Runtime & Manifest-Pinned Artifact Sizing for Von Provider.

Contains verified, manifest-pinned artifact byte sizes and SHA-256 digests
sourced from the canonical Hugging Face repository tree (wfzyx/von).

Reconciliation (Prompt 017, 2026-10-01):
  Selected HF revision: 308feac9c53d4080df31b7d81362bffbd086de2d
  Previous revision (4b2d56a297...) was HTTP 404 on Hugging Face.
  special_tokens_map.json removed: file does not exist in wfzyx/von at any audited commit.
  SHA-256 digests sourced from Git LFS pointer records at the reconciled revision.
  Full on-disk recalculation is required after download before model activation.
"""

from typing import Dict

from src.model_policy.models import (
    ModelArtifactDescriptor,
    ModelRuntimeDescriptor,
    RuntimeFamily,
    SkillResourceDescriptor,
)
from src.model_policy.runtime_registry import ModelRuntimeRegistry

# Exact manifest-pinned artifact sizes from Hugging Face tree
VON_FILES_BY_SIZE: Dict[str, int] = {
    "model.safetensors": 1_583_355_740,
    "option_marker.pt": 1_581_316_050,
    "tokenizer.json": 3_583_485,
    "config.json": 6_349,
    "tokenizer_config.json": 599,
}

VON_EXACT_ARTIFACT_BYTES: int = sum(VON_FILES_BY_SIZE.values())  # Exactly 3,168,262,223 bytes

# Reconciled at Prompt 017: hashes extracted from LFS pointer records at
# HF commit 308feac9c53d4080df31b7d81362bffbd086de2d.
# Full on-disk SHA-256 recalculation required after download before model activation.
VON_EXPECTED_SHA256: Dict[str, str] = {
    "config.json": "968db6072229e54691be4d535487a5c8d8132cc4893b6a3fdfa1e605457a7ec3",
    "tokenizer.json": "3703d011b3ee259e00b59b60772f8fd54998d79e8fa5a1388d507eff28f8f09e",
    "tokenizer_config.json": "57170b72a72c60407eec1f7894000c567309a8e41c5d0504b946c624cb069984",
    "model.safetensors": "349c9b1bf3d5e3217a78c8039606e07069ffae4383640998f99a96bdb6b90c8d",
    "option_marker.pt": "52202f176080d1efe38ca62c45264894fc7e444d332f4af6bac86f4e3146e0f2",
}

TORCH_CUDA12_RUNTIME = ModelRuntimeDescriptor(
    runtime_id="torch-cuda12-2.6",
    runtime_family=RuntimeFamily.PYTORCH_CUDA,
    version="2.6.0+cu124",
    package_dependencies=["torch", "transformers", "tokenizers", "von-sdk"],
    installed_disk_bytes=4_800_000_000,  # ~4.80 GB
    transient_install_bytes=2_800_000_000,  # ~2.80 GB wheel cache
    cuda_capable=True,
    shared_runtime=True,
)

VON_1_1_395M_MODEL = ModelArtifactDescriptor(
    model_id="von-1.1-395m",
    manifest_name="von-1.1-option-marker-395m",
    runtime_id="torch-cuda12-2.6",
    exact_artifact_bytes=VON_EXACT_ARTIFACT_BYTES,
    expected_sha256_hashes=VON_EXPECTED_SHA256,
    context_window_tokens=2048,
)

SKILL_VON_ROUTING = SkillResourceDescriptor(
    skill_id="decision_provider_von",
    required_model_id="von-1.1-395m",
    declared_marginal_ram_bytes=64 * 1024 * 1024,  # 64 MB marginal per active request
    declared_marginal_vram_bytes=32 * 1024 * 1024,  # 32 MB marginal per active request
    marginal_cpu_cores=1.0,
    timeout_ms=500,
    priority=100,
)

SKILL_LIGHTWEIGHT_VALIDATOR = SkillResourceDescriptor(
    skill_id="lightweight_rule_validator",
    required_model_id=None,  # CPU only, no model required
    declared_marginal_ram_bytes=24 * 1024 * 1024,  # 24 MB marginal RAM
    declared_marginal_vram_bytes=0,
    marginal_cpu_cores=0.5,
    timeout_ms=200,
    priority=50,
)


def build_default_model_registry() -> ModelRuntimeRegistry:
    """Instantiate and populate the canonical ModelRuntimeRegistry."""
    registry = ModelRuntimeRegistry()
    registry.register_runtime(TORCH_CUDA12_RUNTIME)
    registry.register_model(VON_1_1_395M_MODEL)
    return registry
