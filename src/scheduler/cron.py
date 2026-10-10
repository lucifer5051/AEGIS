"""SAGE Durable Scheduler -- Deterministic Cron Evaluator.

Canonical Reference:
    docs/specifications/AEGIS_TECHNICAL_FOUNDATION_SPECIFICATION.md § 12
    docs/specifications/AEGIS_DATABASE_SCHEMA_ARCHITECTURE.md § 12

Zero external dependencies; parses standard 5-part cron expressions:
  minute (0-59), hour (0-23), day of month (1-31), month (1-12), day of week (0-6, 0=Sun)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Set

from src.scheduler.errors import ScheduleValidationError


def _parse_field(field_str: str, min_val: int, max_val: int) -> Set[int]:
    """Parse a single cron field into a set of allowed integer values."""
    field_str = field_str.strip()
    if not field_str:
        raise ScheduleValidationError("Empty cron field encountered.")

    values: Set[int] = set()
    for part in field_str.split(","):
        part = part.strip()
        if not part:
            continue

        if "/" in part:
            subparts = part.split("/")
            if len(subparts) != 2:
                raise ScheduleValidationError(f"Invalid step syntax in cron field: '{part}'")
            range_part, step_str = subparts[0].strip(), subparts[1].strip()
            try:
                step = int(step_str)
                if step <= 0:
                    raise ValueError("Step must be positive")
            except ValueError as exc:
                raise ScheduleValidationError(
                    f"Invalid step increment in cron field: '{step_str}'"
                ) from exc

            if range_part == "*":
                start, end = min_val, max_val
            elif "-" in range_part:
                r_start, r_end = range_part.split("-", 1)
                start, end = int(r_start), int(r_end)
            else:
                start, end = int(range_part), max_val

            if start < min_val or end > max_val or start > end:
                raise ScheduleValidationError(f"Invalid range in cron step: '{range_part}'")

            for v in range(start, end + 1, step):
                values.add(v)

        elif "-" in part:
            subparts = part.split("-")
            if len(subparts) != 2:
                raise ScheduleValidationError(f"Invalid range syntax in cron field: '{part}'")
            try:
                start, end = int(subparts[0]), int(subparts[1])
            except ValueError as exc:
                raise ScheduleValidationError(
                    f"Non-integer values in cron range: '{part}'"
                ) from exc

            if start < min_val or end > max_val or start > end:
                raise ScheduleValidationError(
                    f"Range {start}-{end} out of bounds [{min_val}, {max_val}]."
                )
            for v in range(start, end + 1):
                values.add(v)

        elif part == "*":
            for v in range(min_val, max_val + 1):
                values.add(v)

        else:
            try:
                val = int(part)
            except ValueError as exc:
                raise ScheduleValidationError(f"Invalid integer token in cron: '{part}'") from exc

            if val < min_val or val > max_val:
                # Support 7 as Sunday in DOW
                if min_val == 0 and max_val == 6 and val == 7:
                    val = 0
                else:
                    raise ScheduleValidationError(
                        f"Value {val} out of bounds [{min_val}, {max_val}]."
                    )
            values.add(val)

    if not values:
        raise ScheduleValidationError(f"Cron field '{field_str}' yielded empty value set.")
    return values


def compute_next_cron_run(
    cron_expression: str,
    after_time: datetime,
) -> datetime:
    """Compute the next UTC datetime matching a 5-part cron expression after after_time.

    Args:
        cron_expression: Standard 5-part cron (minute hour dom month dow).
        after_time: Starting point (must be timezone-aware UTC).

    Returns:
        Next matching UTC datetime.

    Raises:
        ScheduleValidationError: On malformed cron expression or unmatchable pattern.
    """
    parts = cron_expression.strip().split()
    if len(parts) != 5:
        raise ScheduleValidationError(
            f"Cron expression must have exactly 5 fields, got {len(parts)}: '{cron_expression}'"
        )

    valid_minutes = _parse_field(parts[0], 0, 59)
    valid_hours = _parse_field(parts[1], 0, 23)
    valid_dom = _parse_field(parts[2], 1, 31)
    valid_months = _parse_field(parts[3], 1, 12)
    valid_dow = _parse_field(parts[4], 0, 6)

    # Ensure UTC timezone
    if after_time.tzinfo is None:
        current = after_time.replace(tzinfo=timezone.utc)
    else:
        current = after_time.astimezone(timezone.utc)

    # Truncate seconds/microseconds and advance by 1 minute
    current = current.replace(second=0, microsecond=0) + timedelta(minutes=1)

    max_minutes_to_check = 525600  # 365 days of minutes
    for _ in range(max_minutes_to_check):
        # Month check
        if current.month not in valid_months:
            # Advance to 1st of next month
            if current.month == 12:
                current = current.replace(year=current.year + 1, month=1, day=1, hour=0, minute=0)
            else:
                current = current.replace(month=current.month + 1, day=1, hour=0, minute=0)
            continue

        # Day of month & day of week check
        # Python weekday: 0=Mon, 6=Sun -> convert to 0=Sun, 1=Mon, ..., 6=Sat
        dow_val = (current.weekday() + 1) % 7
        if current.day not in valid_dom or dow_val not in valid_dow:
            current = (current + timedelta(days=1)).replace(hour=0, minute=0)
            continue

        # Hour check
        if current.hour not in valid_hours:
            current = (current + timedelta(hours=1)).replace(minute=0)
            continue

        # Minute check
        if current.minute not in valid_minutes:
            current = current + timedelta(minutes=1)
            continue

        return current

    raise ScheduleValidationError(
        f"No occurrence found within 365 days for cron '{cron_expression}'."
    )
