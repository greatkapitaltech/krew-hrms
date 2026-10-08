"""
attendance/config_tiers.py

Compatibility re-export. The real implementation moved to
base/config_tiers.py once geofencing became a second consumer of the
shared tiered-config mechanism (see that module's docstring). Kept here,
not deleted, because attendance/migrations/0008_attendance_rule_set_and_
pending_config_change.py records `attendance.config_tiers.
TieredConfigResolutionMixin` as a frozen model base -- migration files are
historical and never edited after the fact, so this name must keep
resolving at this import path indefinitely. New code should import
directly from base.config_tiers instead.
"""

from base.config_tiers import (  # noqa: F401
    TIER_CHOICES,
    TIER_COMPANY,
    TIER_DEPARTMENT,
    TIER_EMPLOYEE_TYPE,
    TieredConfigResolutionMixin,
    config_override_applied,
    config_override_cancelled,
    config_override_requested,
    next_month_first,
)
