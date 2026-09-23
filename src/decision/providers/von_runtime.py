"""Canonical Runtime & Manifest-Pinned Artifact Sizing for Von Provider.

Contains verified, manifest-pinned artifact byte sizes and SHA-256 digests
sourced from the canonical Hugging Face repository tree (wfzyx/von).
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

VON_EXPECTED_SHA256: Dict[str, str] = {
    "config.json": "8a32b988f0cb6733ecbe3db622be147b7486847feecab41d13dbff4d1b8276f7",
    "tokenizer.json": "3cb15bf6ae0e729a4a754160ee693b1bc2dc3b0b7407519965d38c6411d51a66",
    "tokenizer_config.json": "df873bc7962450532ff4bc2a9be71e21bca96b86064f26a117d91d9d9bbcb040",
    "special_tokens_map.json": "9b1287c88b0a996da6c8888b13d2f9547d7c6753cb7d29ceca13e71d2b7811bf",
    "model.safetensors": "47a3ff992b8d00344d18ecba1c23a7bb9184fc903d6d6ce0c3c6f8490a6188e4",
    "option_marker.pt": "5bc83769c27f3d6ce7481ba93240212384a28f73e57f5c71b6935272a2490b84",
}

TORCH_CUDA12_RUNTIME = ModelRuntimeDescriptor(
    runtime_id="torch-cuda12-2.6",
    runtime_family=RuntimeFamily.PYTORCH_CUDA,
    version="2.6.0+cu124",
    package_dependencies=["torch", "transformers", "tokenizers", "von-sdk"],
    installed_disk_bytes=4_800_000_000,     # ~4.80 GB
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
    declared_marginal_ram_bytes=64 * 1024 * 1024,   # 64 MB marginal per active request
    declared_marginal_vram_bytes=32 * 1024 * 1024,  # 32 MB marginal per active request
    marginal_cpu_cores=1.0,
    timeout_ms=500,
    priority=100,
)

SKILL_LIGHTWEIGHT_VALIDATOR = SkillResourceDescriptor(
    skill_id="lightweight_rule_validator",
    required_model_id=None,                          # CPU only, no model required
    declared_marginal_ram_bytes=24 * 1024 * 1024,   # 24 MB marginal RAM
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
