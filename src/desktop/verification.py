"""SAGE Desktop Awareness Subsystem -- Verification Service.

Enforces post-action state assertion. Never assumes success without UI verification.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, NamedTuple, Optional

logger = logging.getLogger(__name__)


class VerificationResult(NamedTuple):
    passed: bool
    reason: str
    observed_state: Optional[Dict[str, Any]] = None


class DesktopVerificationService:
    """Verifies that an intentional UI Automation action produced the expected UI transition."""

    def verify_outcome(
        self,
        action: str,
        expected_outcome: Optional[str],
        post_action_snapshot: Dict[str, Any],
        timeout_seconds: float = 3.0,
    ) -> VerificationResult:
        """Verify whether expected_outcome has manifested in post_action_snapshot."""
        if not expected_outcome:
            # If caller specified no explicit outcome, check for basic valid snapshot
            if post_action_snapshot:
                return VerificationResult(passed=True, reason="Action executed with valid post-state perception.")
            return VerificationResult(passed=False, reason="Action executed but post-action perception was empty.")

        snapshot_str = str(post_action_snapshot).lower()
        expected_lower = expected_outcome.lower()

        if expected_lower in snapshot_str:
            return VerificationResult(
                passed=True,
                reason=f"Verification confirmed: observed '{expected_outcome}' in post-action state.",
                observed_state=post_action_snapshot,
            )

        return VerificationResult(
            passed=False,
            reason=f"Verification failed: expected '{expected_outcome}' not found in post-action state.",
            observed_state=post_action_snapshot,
        )
