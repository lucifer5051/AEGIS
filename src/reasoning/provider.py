"""SAGE Local Reasoning Provider - llama-cpp-python Implementation.

Implements the LocalReasoningProvider interface backed by llama-cpp-python.
Enforces all core SAGE architectural invariants:
1. Zero network egress: operates completely offline on local GGUF weights.
2. Storage boundary enforcement: models must reside outside OneDrive, git repo, and C:\\AEGIS.
3. Cryptographic integrity: strictly verifies byte size and SHA-256 before loading.
4. Model resource admission: gates model loading and inference via SkillConcurrencyManager.
5. In-memory residency: tracks resident state and active skill leases via ModelResidencyManager.
6. Fail-closed: all failures transition provider to FAILED/UNLOADED and deny execution.
7. Clean eviction: verifies zero active skill leases before allowing unload.
"""

from __future__ import annotations

import gc
import hashlib
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.model_policy.concurrency_manager import SkillConcurrencyManager
from src.model_policy.live_benchmark import LiveHardwareBenchmark
from src.model_policy.residency_manager import ModelResidencyManager
from src.reasoning.base import LocalReasoningProvider
from src.reasoning.errors import (
    ReasoningAdmissionError,
    ReasoningError,
    ReasoningExecutionError,
    ReasoningIntegrityError,
    ReasoningResidencyError,
    ReasoningStorageError,
)
from src.reasoning.models import (
    ReasoningMetrics,
    ReasoningProviderStatus,
    ReasoningRequest,
    ReasoningResponse,
)
from src.reasoning.runtime import (
    QWEN35_4B_EXACT_BYTES,
    QWEN35_4B_EXPECTED_SHA256,
    QWEN35_4B_FILENAME,
    SKILL_LOCAL_REASONING,
)
from src.storage.paths import (
    get_default_sage_home,
    is_path_inside_onedrive,
    is_path_inside_recovery_repo,
    is_path_inside_repo,
)


def get_default_local_model_dir() -> Path:
    """Return default directory for SAGE local LLM models (%USERPROFILE%\\.aegis\\models)."""
    return get_default_sage_home() / "models" / "local-llm" / "qwen35"


class LlamaCppReasoningProvider(LocalReasoningProvider):
    """Local reasoning provider powered by llama-cpp-python and quantized GGUF weights."""

    def __init__(
        self,
        model_dir: Optional[Path] = None,
        model_filename: str = QWEN35_4B_FILENAME,
        expected_sha256: str = QWEN35_4B_EXPECTED_SHA256,
        expected_bytes: int = QWEN35_4B_EXACT_BYTES,
        concurrency_manager: Optional[SkillConcurrencyManager] = None,
        residency_manager: Optional[ModelResidencyManager] = None,
        benchmark: Optional[LiveHardwareBenchmark] = None,
        n_ctx: int = 4096,
        n_threads: Optional[int] = None,
        n_gpu_layers: int = 0,
        model_id: str = "qwen35-4b-q4km",
        provider_id: str = "llamacpp",
    ) -> None:
        super().__init__(provider_id=provider_id, model_id=model_id)
        self.model_dir = (model_dir or get_default_local_model_dir()).resolve()
        self.model_filename = model_filename
        self.expected_sha256 = expected_sha256
        self.expected_bytes = expected_bytes
        self.concurrency_manager = concurrency_manager
        self.residency_manager = residency_manager
        self.benchmark = benchmark or LiveHardwareBenchmark()
        self.n_ctx = n_ctx
        self.n_threads = n_threads
        self.n_gpu_layers = n_gpu_layers

        self._llm: Any = None
        self._load_latency_ms: float = 0.0
        self._resident_ram_delta: int = 0
        self._total_inferences: int = 0
        self._total_tokens_generated: int = 0
        self._last_inference_latency_ms: float = 0.0
        self._last_first_token_latency_ms: float = 0.0

    @property
    def model_path(self) -> Path:
        """Full resolved path to the local model artifact."""
        return self.model_dir / self.model_filename

    def verify_storage_boundary(self) -> None:
        """Enforce strict storage boundaries: forbidden inside OneDrive, git repo, or recovery repo.

        Raises:
            ReasoningStorageError: If model directory violates forbidden paths.
        """
        if is_path_inside_onedrive(self.model_dir):
            raise ReasoningStorageError(
                f"Storage boundary violation: local model directory '{self.model_dir}' "
                f"resides inside OneDrive."
            )
        if is_path_inside_repo(self.model_dir):
            raise ReasoningStorageError(
                f"Storage boundary violation: local model directory '{self.model_dir}' "
                f"resides inside the source git repository."
            )
        if is_path_inside_recovery_repo(self.model_dir):
            raise ReasoningStorageError(
                f"Storage boundary violation: local model directory '{self.model_dir}' "
                f"resides inside the recovery repository."
            )

    def verify_integrity(self) -> bool:
        """Verify on-disk artifact existence, exact byte size, and cryptographic SHA-256 hash.

        Returns:
            True if all checks pass, False otherwise.
        """
        try:
            self.verify_storage_boundary()
        except ReasoningStorageError:
            return False

        if not self.model_path.is_file():
            return False

        try:
            actual_size = os.path.getsize(self.model_path)
            if actual_size != self.expected_bytes:
                return False

            hasher = hashlib.sha256()
            with open(self.model_path, "rb") as f:
                while chunk := f.read(8 * 1024 * 1024):
                    hasher.update(chunk)
            actual_sha = hasher.hexdigest().upper()
            return actual_sha == self.expected_sha256.upper()
        except Exception:
            return False

    def is_ready(self) -> bool:
        """Return True if model is loaded and ready for inference."""
        return self._status == ReasoningProviderStatus.READY and self._llm is not None

    def initialize(self) -> bool:
        """Preflight, admit, load weights into memory, and transition to READY.

        Returns:
            True if initialized successfully, False otherwise.

        Raises:
            ReasoningStorageError: If path violates storage boundaries.
            ReasoningIntegrityError: If artifact is missing or corrupted.
            ReasoningAdmissionError: If resource admission fails.
            ReasoningExecutionError: If framework initialization fails.
        """
        if self.is_ready():
            return True

        self._status = ReasoningProviderStatus.LOADING

        # 1. Enforce storage boundary
        self.verify_storage_boundary()

        # 2. Enforce cryptographic integrity
        if not self.verify_integrity():
            self._status = ReasoningProviderStatus.FAILED
            raise ReasoningIntegrityError(
                f"Integrity check failed for model '{self.model_id}' at '{self.model_path}'. "
                f"Expected exact size {self.expected_bytes} bytes "
                f"and SHA-256 {self.expected_sha256}."
            )

        # 3. Dynamic RAM admission gate
        if self.concurrency_manager is not None:
            eval_result = self.concurrency_manager.evaluate_skill_admission(SKILL_LOCAL_REASONING)
            if not eval_result.permitted:
                self._status = ReasoningProviderStatus.FAILED
                raise ReasoningAdmissionError(
                    f"Model resource admission denied for '{self.model_id}': {eval_result.reason}"
                )

        # 4. Model loading with residency tracking
        try:
            # Measure baseline memory
            ram_before, _, _, _ = self.benchmark.query_live_memory()

            t_start = time.perf_counter()

            # Import llama_cpp safely
            try:
                from llama_cpp import Llama
            except ImportError as err:
                self._status = ReasoningProviderStatus.FAILED
                raise ReasoningExecutionError(
                    f"llama-cpp-python is not installed in the environment: {err}"
                ) from err

            # Load model instance
            self._llm = Llama(
                model_path=str(self.model_path),
                n_ctx=self.n_ctx,
                n_threads=self.n_threads,
                n_gpu_layers=self.n_gpu_layers,
                verbose=False,
            )

            self._load_latency_ms = (time.perf_counter() - t_start) * 1000.0

            ram_after, _, _, _ = self.benchmark.query_live_memory()
            self._resident_ram_delta = max(0, ram_before - ram_after)

            # Mark resident in residency manager
            if self.residency_manager is not None:
                self.residency_manager.set_resident(self.model_id, resident=True)

            self._status = ReasoningProviderStatus.READY
            return True

        except Exception as exc:
            self._status = ReasoningProviderStatus.FAILED
            if self.residency_manager is not None:
                self.residency_manager.unload_model(self.model_id)
            if isinstance(exc, (ReasoningError, ReasoningExecutionError)):
                raise
            raise ReasoningExecutionError(
                f"Failed to load local reasoning model '{self.model_id}': {exc}"
            ) from exc

    def reason(self, request: ReasoningRequest) -> ReasoningResponse:
        """Execute a deterministic local reasoning query.

        Args:
            request: Typed reasoning request.

        Returns:
            Typed reasoning response with timing and token usage.

        Raises:
            ReasoningExecutionError: If provider is not ready or generation fails.
            ReasoningAdmissionError: If lease cannot be acquired.
        """
        if not self.is_ready():
            # Try to initialize
            self.initialize()

        lease = None
        if self.concurrency_manager is not None:
            lease = self.concurrency_manager.acquire_skill_lease(SKILL_LOCAL_REASONING)
            if lease is None:
                raise ReasoningAdmissionError(
                    f"Failed to acquire skill execution lease for reasoning skill "
                    f"'{SKILL_LOCAL_REASONING.skill_id}'."
                )

        try:
            # Build messages
            messages: List[Dict[str, str]] = []
            if request.system_prompt:
                messages.append({"role": "system", "content": request.system_prompt})
            messages.append({"role": "user", "content": request.prompt})

            t_infer_start = time.perf_counter()
            t_first_token: Optional[float] = None
            generated_chunks: List[str] = []
            finish_reason = "stop"

            # Stream to capture Time-To-First-Token (TTFT)
            chat_kwargs: Dict[str, Any] = {
                "messages": messages,
                "max_tokens": request.max_tokens,
                "temperature": request.temperature,
                "top_p": request.top_p,
                "stop": request.stop_sequences if request.stop_sequences else None,
                "stream": True,
            }
            if request.response_format:
                chat_kwargs["response_format"] = request.response_format

            stream_gen = self._llm.create_chat_completion(**chat_kwargs)

            for chunk in stream_gen:
                choices = chunk.get("choices", [])
                if not choices:
                    continue
                choice = choices[0]
                delta_content = choice.get("delta", {}).get("content", "")
                if delta_content:
                    if t_first_token is None:
                        t_first_token = time.perf_counter()
                    generated_chunks.append(delta_content)
                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]

            t_infer_end = time.perf_counter()

            infer_duration_ms = (t_infer_end - t_infer_start) * 1000.0
            first_token_duration_ms = (
                (t_first_token - t_infer_start) * 1000.0 if t_first_token else infer_duration_ms
            )

            content = "".join(generated_chunks).strip()

            # Approximate tokens if tokenizer counts are not stream-returned
            prompt_tokens = len(self._llm.tokenize(request.prompt.encode("utf-8")))
            completion_tokens = len(self._llm.tokenize(content.encode("utf-8")))
            total_tokens = prompt_tokens + completion_tokens

            # Update metrics
            self._total_inferences += 1
            self._total_tokens_generated += completion_tokens
            self._last_inference_latency_ms = infer_duration_ms
            self._last_first_token_latency_ms = first_token_duration_ms

            return ReasoningResponse(
                content=content,
                finish_reason=finish_reason,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                load_duration_ms=self._load_latency_ms,
                inference_duration_ms=infer_duration_ms,
                first_token_duration_ms=first_token_duration_ms,
                model_id=self.model_id,
                provider_id=self.provider_id,
            )

        except Exception as exc:
            if isinstance(exc, (ReasoningError, ReasoningAdmissionError)):
                raise
            raise ReasoningExecutionError(
                f"Local reasoning inference failed on model '{self.model_id}': {exc}"
            ) from exc

        finally:
            if lease is not None and self.concurrency_manager is not None:
                self.concurrency_manager.release_skill_lease(lease)

    def unload(self) -> bool:
        """Evict model from memory safely, respecting active skill leases.

        Returns:
            True if unloaded, False if not unloaded.

        Raises:
            ReasoningResidencyError: If active skill leases prevent eviction.
        """
        if self._llm is None:
            self._status = ReasoningProviderStatus.UNLOADED
            return True

        if self.residency_manager is not None:
            if not self.residency_manager.can_unload(self.model_id):
                active_leases = self.residency_manager.get_active_lease_count(self.model_id)
                raise ReasoningResidencyError(
                    f"Cannot unload model '{self.model_id}': {active_leases} active "
                    f"skill leases remain attached."
                )
            self.residency_manager.unload_model(self.model_id)

        # Free C++ model instance
        del self._llm
        self._llm = None
        gc.collect()

        self._status = ReasoningProviderStatus.UNLOADED
        return True

    def get_metrics(self) -> ReasoningMetrics:
        """Return cumulative operational metrics."""
        return ReasoningMetrics(
            total_inferences=self._total_inferences,
            total_tokens_generated=self._total_tokens_generated,
            load_latency_ms=self._load_latency_ms,
            last_inference_latency_ms=self._last_inference_latency_ms,
            last_first_token_latency_ms=self._last_first_token_latency_ms,
            resident_ram_delta_bytes=self._resident_ram_delta,
        )
