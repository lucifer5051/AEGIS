"""SAGE Local Reasoning Runtime & Artifact Descriptors.

Defines the canonical llama.cpp framework runtime, the verified local reasoning
model descriptor, and the skill resource profile for manager reasoning.
"""

from typing import Dict

from src.model_policy.models import (
    ModelArtifactDescriptor,
    ModelRuntimeDescriptor,
    RuntimeFamily,
    SkillResourceDescriptor,
)
from src.model_policy.runtime_registry import ModelRuntimeRegistry

# Exact manifest-pinned artifact properties
QWEN35_4B_FILENAME = "qwen35-4b-q4km.gguf"
QWEN35_4B_EXACT_BYTES = 2_740_937_888
QWEN35_4B_EXPECTED_SHA256 = "00FE7986FF5F6B463E62455821146049DB6F9313603938A70800D1FB69EF11A4"

QWEN35_FILES_BY_SIZE: Dict[str, int] = {
    QWEN35_4B_FILENAME: QWEN35_4B_EXACT_BYTES,
}

QWEN35_EXPECTED_SHA256: Dict[str, str] = {
    QWEN35_4B_FILENAME: QWEN35_4B_EXPECTED_SHA256,
}

# Runtime specification for llama.cpp CPU
LLAMACPP_CPU_RUNTIME = ModelRuntimeDescriptor(
    runtime_id="llamacpp-cpu-0.3",
    runtime_family=RuntimeFamily.LLAMACPP,
    version="0.3.36",
    package_dependencies=["llama-cpp-python"],
    installed_disk_bytes=45_000_000,  # ~45 MB wheel footprint
    transient_install_bytes=80_000_000,  # ~80 MB download/cache
    cuda_capable=False,
    shared_runtime=True,
)

# Model artifact descriptor
QWEN35_4B_MODEL = ModelArtifactDescriptor(
    model_id="qwen35-4b-q4km",
    manifest_name="qwen35-4b-q4km",
    runtime_id="llamacpp-cpu-0.3",
    exact_artifact_bytes=QWEN35_4B_EXACT_BYTES,
    expected_sha256_hashes=QWEN35_EXPECTED_SHA256,
    context_window_tokens=4096,  # SAGE default ceiling for local reasoning
)

# Skill resource descriptor for local reasoning inference
SKILL_LOCAL_REASONING = SkillResourceDescriptor(
    skill_id="manager_local_reasoning",
    required_model_id="qwen35-4b-q4km",
    declared_marginal_ram_bytes=64 * 1024 * 1024,  # 64 MB marginal RAM per reasoning query
    declared_marginal_vram_bytes=0,  # CPU execution
    marginal_cpu_cores=2.0,
    timeout_ms=30000,  # 30s timeout for local reasoning
    priority=200,
)


def register_reasoning_models(registry: ModelRuntimeRegistry) -> None:
    """Register the local reasoning runtime and model in the runtime registry."""
    if registry.get_runtime(LLAMACPP_CPU_RUNTIME.runtime_id) is None:
        registry.register_runtime(LLAMACPP_CPU_RUNTIME)
    if registry.get_model(QWEN35_4B_MODEL.model_id) is None:
        registry.register_model(QWEN35_4B_MODEL)
