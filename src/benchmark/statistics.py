"""Deterministic statistical utilities for SAGE benchmark repetitions."""

import math
from typing import List

from src.benchmark.models import StabilityState


def compute_mean(values: List[float]) -> float:
    """Calculate arithmetic mean of a float sequence."""
    if not values:
        return 0.0
    return sum(values) / len(values)


def compute_median(values: List[float]) -> float:
    """Calculate median of a float sequence."""
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    mid = n // 2
    if n % 2 == 1:
        return sorted_vals[mid]
    return (sorted_vals[mid - 1] + sorted_vals[mid]) / 2.0


def compute_stdev(values: List[float]) -> float:
    """Calculate sample standard deviation."""
    if len(values) < 2:
        return 0.0
    avg = compute_mean(values)
    var = sum((x - avg) ** 2 for x in values) / (len(values) - 1)
    return math.sqrt(var)


def compute_cv(values: List[float]) -> float:
    """Calculate coefficient of variation (stdev / mean)."""
    if not values:
        return 0.0
    avg = compute_mean(values)
    if avg == 0.0:
        return 0.0
    return compute_stdev(values) / avg


def evaluate_stability(values: List[float], failure_count: int = 0) -> StabilityState:
    """Classify measurement stability based on CV and failure occurrences."""
    if failure_count > 0:
        return StabilityState.CONSTRAINED
    if len(values) < 2:
        return StabilityState.STABLE

    cv = compute_cv(values)
    if cv <= 0.08:
        return StabilityState.STABLE
    if cv <= 0.20:
        return StabilityState.VARIABLE
    return StabilityState.CONSTRAINED
