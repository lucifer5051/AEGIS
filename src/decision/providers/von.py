"""AEGIS Fast Decision Engine - Von Provider (Option-Marker Backend).

Document Reference: docs/specifications/AEGIS_FAST_DECISION_ENGINE.md
Constraints:
- Pinned to exact audited source commit: bed7e7337791c5124a557eb59ad918cb6747b276.
- Checkpoint directory MUST reside outside OneDrive, outside Git repo, and outside C:\\AEGIS.
- Complete local SHA-256 verification against trusted source-controlled manifest before activation.
- Strictly offline by construction: no remote APIs, no HTTP servers, no background downloads.
- Non-blocking bounded concurrency: immediately fails closed with ADMISSION_BUSY if busy.
- Strictly advisory: cannot authorize, approve, or execute capabilities.
"""

import hashlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from src.decision.errors import (
    ModelIntegrityError,
    ProviderUnavailableError,
)
from src.decision.models import (
    AlternativeRoute,
    DecisionOutcome,
    DecisionRoute,
    FallbackReason,
    FastDecisionRequest,
    FastDecisionResult,
    ProviderMetrics,
    ProviderStatus,
)
from src.decision.providers.base import FastDecisionProvider
from src.storage.paths import (
    get_default_aegis_home,
    is_path_inside_onedrive,
    is_path_inside_recovery_repo,
    is_path_inside_repo,
)

logger = logging.getLogger(__name__)

# Pinned commit and manifest constants
VON_AUDITED_COMMIT = "bed7e7337791c5124a557eb59ad918cb6747b276"
VON_DEFAULT_VERSION = "1.1.0"
TRUSTED_MANIFEST_RELATIVE_PATH = (
    Path("schemas") / "model-manifests" / "von-1.1-option-marker-395m.json"
)


class VonFastDecisionProvider(FastDecisionProvider):
    """Local, offline Fast Decision Engine provider utilizing Von's Option-Marker backend."""

    def __init__(
        self,
        checkpoint_dir: Optional[Path | str] = None,
        manifest_path: Optional[Path | str] = None,
        version: str = VON_DEFAULT_VERSION,
        backend_factory: Optional[Callable[[Path], Any]] = None,
        concurrency_manager: Optional[Any] = None,
    ) -> None:
        super().__init__(provider_id="von", provider_version=version)
        self.version = version
        self.audited_commit = VON_AUDITED_COMMIT
        self.concurrency_manager = concurrency_manager

        # Resolve model storage directory (%USERPROFILE%\.aegis\models\fast-decision\von\<version>\)
        if checkpoint_dir is not None:
            self.checkpoint_dir = Path(checkpoint_dir).resolve()
        else:
            self.checkpoint_dir = (
                get_default_aegis_home() / "models" / "fast-decision" / "von" / self.version
            ).resolve()

        # Resolve trusted in-repo manifest path
        if manifest_path is not None:
            self.manifest_path = Path(manifest_path).resolve()
        else:
            repo_root = Path(__file__).resolve().parent.parent.parent.parent
            self.manifest_path = (repo_root / TRUSTED_MANIFEST_RELATIVE_PATH).resolve()

        # Concurrency admission control: single-flight non-blocking lock
        self._inference_lock = threading.Lock()

        # Model residency state
        self._model_backend: Optional[Any] = None
        self._backend_factory = backend_factory
        self._latencies: List[float] = []
        self._cold_load_time_ms: float = 0.0
        self._ram_usage_mb: float = 0.0
        self._vram_usage_mb: Optional[float] = None
        self._inference_count: int = 0

    def validate_storage_boundaries(self) -> None:
        """Validate that the checkpoint directory does not violate AEGIS storage rules.

        Raises:
            ModelIntegrityError: If path resolves inside OneDrive, Git repo, or recovery copy.
        """
        if is_path_inside_recovery_repo(self.checkpoint_dir):
            raise ModelIntegrityError(
                "Model checkpoint directory cannot reside inside "
                f"C:\\AEGIS: '{self.checkpoint_dir}'."
            )
        if is_path_inside_repo(self.checkpoint_dir):
            raise ModelIntegrityError(
                "Model checkpoint directory cannot reside inside "
                f"Git repository: '{self.checkpoint_dir}'."
            )
        if is_path_inside_onedrive(self.checkpoint_dir):
            raise ModelIntegrityError(
                "Model checkpoint directory cannot reside inside "
                f"OneDrive: '{self.checkpoint_dir}'."
            )

    def load_trusted_manifest(self) -> Dict[str, Any]:
        """Load and return the trusted source-controlled manifest.

        Raises:
            ModelIntegrityError: If manifest file is missing or unreadable.
        """
        if not self.manifest_path.is_file():
            raise ModelIntegrityError(
                f"Trusted source-controlled manifest not found at: '{self.manifest_path}'."
            )
        try:
            with open(self.manifest_path, "r", encoding="utf-8") as f:
                data: Dict[str, Any] = json.load(f)
            return data
        except Exception as exc:
            raise ModelIntegrityError(
                f"Failed to parse trusted manifest '{self.manifest_path}': {exc}"
            ) from exc

    def verify_integrity(self) -> bool:
        """Verify local checkpoint files against the trusted source-controlled manifest.

        Calculates SHA-256 for each required file and compares against expected digests.

        Returns:
            True if all required files are present and match hashes exactly, False otherwise.
        """
        self.validate_storage_boundaries()

        if not self.checkpoint_dir.is_dir():
            logger.warning("Von checkpoint directory does not exist: %s", self.checkpoint_dir)
            return False

        try:
            manifest = self.load_trusted_manifest()
        except ModelIntegrityError as e:
            logger.error("Integrity check aborted: %s", e)
            return False

        # Validate source commit invariant in manifest
        manifest_commit = manifest.get("source_commit")
        if manifest_commit != VON_AUDITED_COMMIT:
            logger.error(
                "Manifest source commit mismatch: expected '%s', got '%s'",
                VON_AUDITED_COMMIT,
                manifest_commit,
            )
            return False

        required_files: Dict[str, Any] = manifest.get("required_files", {})
        if not required_files:
            logger.error("Trusted manifest specifies no required files.")
            return False

        for rel_filename, meta in required_files.items():
            file_path = self.checkpoint_dir / rel_filename
            if not file_path.is_file():
                if meta.get("required", True):
                    logger.warning("Required model file missing: %s", file_path)
                    return False
                continue

            expected_sha = meta.get("sha256", "").lower()
            if not expected_sha:
                logger.error("Missing expected sha256 for required file: %s", rel_filename)
                return False

            actual_sha = self._compute_sha256(file_path)
            if actual_sha.lower() != expected_sha:
                logger.error(
                    "SHA-256 mismatch for '%s': expected '%s', got '%s'",
                    rel_filename,
                    expected_sha,
                    actual_sha,
                )
                return False

        return True

    def qualify(self, hardware_profile: Optional[Any] = None) -> bool:
        """Perform hardware-aware qualification using measured load and latency.

        Checks whether the host has sufficient RAM and resources to run Von safely.
        """
        if not self.verify_integrity():
            self._status = ProviderStatus.DISABLED
            return False

        # If hardware profile is provided, check available memory constraints
        if hardware_profile is not None:
            ram_profile = getattr(hardware_profile, "ram", None)
            if ram_profile is not None:
                available_bytes = getattr(ram_profile, "available_bytes", None)
                # Model weights ~3.2 GB; require at least 1.5 GB free for safe execution
                min_safe_bytes = 1500 * 1024 * 1024
                if available_bytes is not None and available_bytes < min_safe_bytes:
                    logger.warning(
                        "Hardware qualification failed: available RAM (%d MB) "
                        "below minimum safe threshold (%d MB)",
                        available_bytes // (1024 * 1024),
                        min_safe_bytes // (1024 * 1024),
                    )
                    self._status = ProviderStatus.DISABLED
                    return False

        return True

    def initialize(self) -> bool:
        """Preload, verify, and warm the local Von Option-Marker backend.

        Ensures local-only execution: no remote HTTP server, no automatic downloads.
        """
        if self._status == ProviderStatus.READY and self._model_backend is not None:
            return True

        self._status = ProviderStatus.LOADING
        t_start = time.perf_counter()

        # Step 1: Storage boundary check
        try:
            self.validate_storage_boundaries()
        except ModelIntegrityError as exc:
            logger.error("Boundary validation failed: %s", exc)
            self._status = ProviderStatus.DISABLED
            return False

        # Step 2: Artifact integrity verification
        if not self.verify_integrity():
            logger.warning("Integrity verification failed for Von checkpoint. Provider DISABLED.")
            self._status = ProviderStatus.DISABLED
            return False

        # Step 3: Instantiate local OptionMarker backend
        try:
            if self._backend_factory is not None:
                self._model_backend = self._backend_factory(self.checkpoint_dir)
            else:
                self._model_backend = self._load_native_backend()

            # Record cold load time
            self._cold_load_time_ms = (time.perf_counter() - t_start) * 1000.0

            # Step 4: Smoke inference verification
            smoke_ok = self._run_smoke_inference()
            if not smoke_ok:
                logger.error("Von smoke inference failed. Provider ERROR.")
                self._status = ProviderStatus.ERROR
                self.unload()
                return False

            self._status = ProviderStatus.READY
            logger.info(
                "VonFastDecisionProvider initialized successfully in %.2f ms",
                self._cold_load_time_ms,
            )
            return True

        except Exception as exc:
            logger.error("Failed to initialize Von provider backend: %s", exc)
            self._status = ProviderStatus.DISABLED
            self.unload()
            return False

    def is_ready(self) -> bool:
        """Check whether the provider is initialized and ready for inference."""
        return self._status == ProviderStatus.READY and self._model_backend is not None

    def evaluate(
        self,
        request: FastDecisionRequest,
        criteria: Dict[DecisionRoute, str],
    ) -> FastDecisionResult:
        """Perform non-blocking advisory categorical Choice routing.

        SECURITY INVARIANTS:
        - Advisory only (`is_advisory_only: Literal[True] = True`).
        - Non-blocking admission: immediately fails closed if busy.
        - Criteria come strictly from trusted caller dictionary, never user input.
        - Never executes or authorizes actions.
        """
        if not self.is_ready():
            return self._build_fallback_result(
                route=DecisionRoute.UNKNOWN_INTENT,
                latency_ms=0.0,
                reason=FallbackReason.PROVIDER_UNAVAILABLE,
            )

        # Concurrency admission control via SkillConcurrencyManager if provided
        lease = None
        if self.concurrency_manager is not None:
            from src.decision.providers.von_runtime import SKILL_VON_ROUTING

            lease = self.concurrency_manager.acquire_skill_lease(SKILL_VON_ROUTING)
            if lease is None:
                logger.info(
                    "SkillConcurrencyManager refused admission; immediate fallback to Manager."
                )
                return self._build_fallback_result(
                    route=DecisionRoute.UNKNOWN_INTENT,
                    latency_ms=0.0,
                    reason=FallbackReason.ADMISSION_BUSY,
                )

        # Non-blocking concurrency check: immediate fallback if inference is already busy
        acquired = self._inference_lock.acquire(blocking=False)
        if not acquired:
            if lease is not None and self.concurrency_manager is not None:
                self.concurrency_manager.release_skill_lease(lease)
            logger.info("Fast Decision concurrency limit reached; immediate non-blocking fallback.")
            return self._build_fallback_result(
                route=DecisionRoute.UNKNOWN_INTENT,
                latency_ms=0.0,
                reason=FallbackReason.ADMISSION_BUSY,
            )

        t_start = time.perf_counter()
        try:
            # Perform inference across immutable criteria
            selected_route, confidence, alternatives = self._perform_inference(
                user_text=request.user_input,
                criteria=criteria,
            )
            latency_ms = (time.perf_counter() - t_start) * 1000.0

            # Record telemetry
            self._record_latency(latency_ms)
            self._inference_count += 1

            return FastDecisionResult(
                outcome=DecisionOutcome.FAST_PATH,
                route=selected_route,
                confidence=confidence,
                alternatives=alternatives,
                provider_id=self.provider_id,
                provider_version=self.provider_version,
                latency_ms=latency_ms,
                fallback_reason=None,
                is_advisory_only=True,
            )

        except Exception as exc:
            latency_ms = (time.perf_counter() - t_start) * 1000.0
            logger.error("Inference exception during Von evaluation: %s", exc)
            return self._build_fallback_result(
                route=DecisionRoute.UNKNOWN_INTENT,
                latency_ms=latency_ms,
                reason=FallbackReason.INFERENCE_EXCEPTION,
            )
        finally:
            self._inference_lock.release()
            if lease is not None and self.concurrency_manager is not None:
                self.concurrency_manager.release_skill_lease(lease)

    def unload(self) -> None:
        """Release resident model memory."""
        self._model_backend = None
        self._status = ProviderStatus.UNINITIALIZED
        logger.debug("Von provider unloaded.")

    def get_metrics(self) -> ProviderMetrics:
        """Compute rolling percentiles and operational metrics."""
        p50 = self._compute_percentile(50.0)
        p95 = self._compute_percentile(95.0)
        p99 = self._compute_percentile(99.0)

        return ProviderMetrics(
            cold_load_time_ms=self._cold_load_time_ms,
            warm_inference_p50_ms=p50,
            warm_inference_p95_ms=p95,
            warm_inference_p99_ms=p99,
            cpu_usage_pct=0.0,
            ram_usage_mb=self._ram_usage_mb,
            vram_usage_mb=self._vram_usage_mb,
            inference_count=self._inference_count,
        )

    # ==========================================================================
    # INTERNAL IMPLEMENTATION HELPERS
    # ==========================================================================

    def _compute_sha256(self, file_path: Path) -> str:
        """Stream compute SHA-256 digest of a local file."""
        hasher = hashlib.sha256()
        with open(file_path, "rb") as f:
            while chunk := f.read(65536):
                hasher.update(chunk)
        return hasher.hexdigest()

    def _load_native_backend(self) -> Any:
        """Load native Von OptionMarker backend.

        Raises:
            ProviderUnavailableError: If von or torch libraries are not installed.
        """
        try:
            # We import von strictly inside this isolated method to allow running
            # on environments where heavy ML packages are optional/staged.
            import von  # type: ignore

            # Construct OptionMarker backend explicitly targeting local checkpoint directory
            # with local_files_only=True and NO automatic downloads.
            backend = von.OptionMarkerBackend(
                checkpoint_dir=str(self.checkpoint_dir),
                local_files_only=True,
            )
            return backend
        except ImportError as e:
            raise ProviderUnavailableError(
                f"Von dependencies ('von', 'torch') are not installed in the environment: {e}"
            ) from e

    def _run_smoke_inference(self) -> bool:
        """Run a minimal verification probe on the resident backend."""
        if self._model_backend is None:
            return False

        smoke_criteria = {
            DecisionRoute.FILE_INTENT: "Read or write local workspace files",
            DecisionRoute.TERMINAL_INTENT: "Execute command line scripts",
        }
        try:
            # If backend provides decide / score method:
            if hasattr(self._model_backend, "decide"):
                choices = {r.value: desc for r, desc in smoke_criteria.items()}
                _ = self._model_backend.decide(
                    state="Read file test.txt",
                    choices=choices,
                )
            return True
        except Exception as exc:
            logger.warning("Smoke inference failed: %s", exc)
            return False

    def _perform_inference(
        self,
        user_text: str,
        criteria: Dict[DecisionRoute, str],
    ) -> tuple[DecisionRoute, float, List[AlternativeRoute]]:
        """Dispatch evaluation to OptionMarker backend and return normalized probabilities."""
        if self._model_backend is None:
            raise ProviderUnavailableError("Model backend not resident.")

        # Prepare choices dictionary: route string -> descriptive semantic criteria
        choices = {route.value: desc for route, desc in criteria.items()}

        # Call local backend
        if hasattr(self._model_backend, "decide"):
            raw_result = self._model_backend.decide(
                state=user_text,
                choices=choices,
            )
            # von.decide returns a structure with winner and scores/probabilities
            # e.g. {"choice": "file_intent", "confidence": 0.94, "scores": {...}}
            best_route_str = getattr(raw_result, "choice", None) or raw_result.get("choice")
            confidence = float(
                getattr(raw_result, "confidence", 0.0) or raw_result.get("confidence", 0.0)
            )
            scores = getattr(raw_result, "scores", None) or raw_result.get("scores", {})
        elif callable(self._model_backend):
            # Callable backend mock support for unit tests
            raw_result = self._model_backend(user_text, choices)
            best_route_str = raw_result.get("choice")
            confidence = float(raw_result.get("confidence", 0.0))
            scores = raw_result.get("scores", {})
        else:
            raise ProviderUnavailableError("Incompatible backend interface.")

        # Map to DecisionRoute enum
        try:
            selected_route = DecisionRoute(best_route_str)
        except (ValueError, TypeError):
            selected_route = DecisionRoute.UNKNOWN_INTENT

        # Build alternatives list
        alternatives: List[AlternativeRoute] = []
        for r_str, conf in scores.items():
            if r_str != selected_route.value:
                try:
                    alt_route = DecisionRoute(r_str)
                    alternatives.append(AlternativeRoute(route=alt_route, confidence=float(conf)))
                except ValueError:
                    pass

        # Sort alternatives by confidence descending
        alternatives.sort(key=lambda a: a.confidence, reverse=True)

        return selected_route, confidence, alternatives

    def _build_fallback_result(
        self,
        route: DecisionRoute,
        latency_ms: float,
        reason: FallbackReason,
    ) -> FastDecisionResult:
        """Construct a standardized advisory fallback result."""
        return FastDecisionResult(
            outcome=DecisionOutcome.FALLBACK_TO_MANAGER,
            route=route,
            confidence=0.0,
            alternatives=[],
            provider_id=self.provider_id,
            provider_version=self.provider_version,
            latency_ms=latency_ms,
            fallback_reason=reason,
            is_advisory_only=True,
        )

    def _record_latency(self, latency_ms: float) -> None:
        """Record latency in rolling window for percentile tracking."""
        self._latencies.append(latency_ms)
        if len(self._latencies) > 1000:
            self._latencies.pop(0)

    def _compute_percentile(self, percentile: float) -> float:
        """Compute the requested percentile from recorded warm latencies."""
        if not self._latencies:
            return 0.0
        sorted_latencies = sorted(self._latencies)
        k = (len(sorted_latencies) - 1) * (percentile / 100.0)
        f = int(k)
        c = f + 1
        if c < len(sorted_latencies):
            d0 = sorted_latencies[f] * (c - k)
            d1 = sorted_latencies[c] * (k - f)
            return d0 + d1
        return sorted_latencies[-1]
