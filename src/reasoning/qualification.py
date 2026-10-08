"""SAGE Local Reasoning Subsystem - Hardware Qualification Integration.

Provides empirical qualification for local reasoning models using the SAGE
ModelHardwareQualifier subsystem. Measures cold load latency, resident memory delta,
and warm inference latency on the live host hardware.
Persists qualification profiles to %USERPROFILE%\\.aegis\\models\\qualifications\\.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from src.model_policy.live_benchmark import LiveHardwareBenchmark
from src.model_policy.models import ModelHardwareQualificationProfile
from src.model_policy.qualification import ModelHardwareQualifier
from src.reasoning.models import ReasoningRequest
from src.reasoning.provider import LlamaCppReasoningProvider
from src.storage.paths import get_default_sage_home


def get_default_qualification_dir() -> Path:
    """Return default directory for model qualification profiles (%USERPROFILE%\\.aegis\\models)."""
    return get_default_sage_home() / "models" / "qualifications"


def qualify_local_reasoning_provider(
    provider: LlamaCppReasoningProvider,
    qualification_dir: Optional[Path] = None,
    benchmark: Optional[LiveHardwareBenchmark] = None,
) -> ModelHardwareQualificationProfile:
    """Execute live empirical hardware qualification for the local reasoning provider.

    Args:
        provider: Unloaded or ready LlamaCppReasoningProvider instance.
        qualification_dir: Destination directory for qualification JSON.
        benchmark: Live hardware benchmark instance.

    Returns:
        Persisted ModelHardwareQualificationProfile with empirical measurements.
    """
    target_dir = qualification_dir or get_default_qualification_dir()
    qualifier = ModelHardwareQualifier(
        benchmark=benchmark or provider.benchmark,
        qualification_dir=target_dir,
    )

    def probe_load() -> None:
        if not provider.is_ready():
            provider.initialize()

    def probe_infer() -> float:
        req = ReasoningRequest(
            prompt="Compute 2 + 2. Respond only with the number.",
            max_tokens=8,
            temperature=0.0,
        )
        resp = provider.reason(req)
        return resp.inference_duration_ms

    profile = qualifier.qualify_model(
        model_id=provider.model_id,
        manifest_name=provider.model_id,
        probe_load_fn=probe_load,
        probe_inference_fn=probe_infer,
    )

    return profile
