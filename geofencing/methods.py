"""
geofencing/methods.py

check_geo_fence() replaces the old ClockInAPIView/ClockOutAPIView
integration, which called GeoFencingEmployeeLocationCheckAPIView.post()
as a bare method (bypassing DRF's own exception handling) inside a
plain `try: ... except: pass` -- so a genuinely-outside result was
correctly rejected, but anything that merely *failed to check*
(missing GPS fix, no boundary configured, a computation error) raised
an exception that got silently swallowed, letting the punch through
completely unflagged and invisible.

This function never raises. Every outcome -- allowed, rejected, a
confirmed violation, or a check that couldn't run -- comes back as data
on GeoCheckResult, so the caller can decide what to do (and what to
persist) without needing a try/except of its own.
"""

from dataclasses import dataclass

from geopy.distance import geodesic

from .models import GeoFencing


@dataclass
class GeoCheckResult:
    allowed: bool
    violation: bool = False  # confirmed outside the boundary
    unverified: bool = False  # boundary configured, but couldn't check it
    reason: str = ""  # for the caller's error Response, when not allowed


def check_geo_fence(employee, latitude, longitude):
    """
    Geo-tag (unconditional, no opt-out: a mobile punch with no GPS fix at
    all can never go through, whether or not Geo-mark boundary
    enforcement is even configured for this company) followed by
    Geo-mark (the employee's resolved GeoFencing row, if any).
    """
    if latitude is None or longitude is None:
        return GeoCheckResult(allowed=False, reason="missing_location")

    rule = GeoFencing.resolve_for_employee(employee)
    if rule is None or not rule.start or rule.is_exemption():
        return GeoCheckResult(allowed=True)

    try:
        distance = geodesic(
            (rule.latitude, rule.longitude), (float(latitude), float(longitude))
        ).meters
    except (TypeError, ValueError):
        # Coordinates were present but not usable (e.g. a malformed
        # payload) -- this is a "couldn't verify" outcome, not "outside."
        return GeoCheckResult(allowed=True, unverified=True)

    if distance <= rule.radius_in_meters:
        return GeoCheckResult(allowed=True)

    if rule.enforcement_mode == GeoFencing.ENFORCEMENT_REJECT:
        return GeoCheckResult(allowed=False, violation=True, reason="outside_boundary")

    return GeoCheckResult(allowed=True, violation=True)
