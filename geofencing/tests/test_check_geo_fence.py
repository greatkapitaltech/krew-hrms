"""
Tests for geofencing/methods.py::check_geo_fence(), which replaced the
old ClockInAPIView/ClockOutAPIView integration -- see the module's own
docstring for exactly what bug this replaces (a verification failure
used to raise and get silently swallowed by a bare except, letting the
punch through completely unflagged).
"""

from unittest.mock import MagicMock, patch

from django.test import TestCase

from base.config_tiers import TIER_COMPANY, TIER_DEPARTMENT, TIER_EMPLOYEE_TYPE
from base.models import COLLAR_WHITE, Department, EmployeeType
from geofencing.methods import check_geo_fence
from geofencing.models import GeoFencing
from horilla.testkit.factories import make_company, make_employee, make_user


@patch("geofencing.models.Nominatim")
class CheckGeoFenceTests(TestCase):
    def setUp(self):
        self.company = make_company("Acme")
        self.employee = make_employee(
            company=self.company, email="check1@test.horilla", user=make_user("check1"),
        )

    def _mock_geocoder(self, nominatim_cls):
        nominatim_cls.return_value.reverse.return_value = MagicMock()

    def test_missing_coordinates_are_rejected_even_with_no_boundary_configured(
        self, nominatim_cls
    ):
        # Geo-tag's own unconditional rule -- not Geo-mark's.
        result = check_geo_fence(self.employee, None, None)
        self.assertFalse(result.allowed)
        self.assertEqual(result.reason, "missing_location")

    def test_no_boundary_configured_allows_the_punch(self, nominatim_cls):
        result = check_geo_fence(self.employee, 1.0, 1.0)
        self.assertTrue(result.allowed)
        self.assertFalse(result.violation)
        self.assertFalse(result.unverified)

    def test_boundary_configured_but_not_started_allows_the_punch(self, nominatim_cls):
        self._mock_geocoder(nominatim_cls)
        GeoFencing.objects.create(
            tier=TIER_COMPANY, company=self.company,
            latitude=1.0, longitude=1.0, radius_in_meters=100, start=False,
        )
        result = check_geo_fence(self.employee, 50.0, 50.0)
        self.assertTrue(result.allowed)
        self.assertFalse(result.violation)

    def test_inside_the_boundary_is_allowed(self, nominatim_cls):
        self._mock_geocoder(nominatim_cls)
        GeoFencing.objects.create(
            tier=TIER_COMPANY, company=self.company,
            latitude=12.9716, longitude=77.5946, radius_in_meters=1000, start=True,
        )
        result = check_geo_fence(self.employee, 12.9716, 77.5946)
        self.assertTrue(result.allowed)
        self.assertFalse(result.violation)
        self.assertFalse(result.unverified)

    def test_outside_the_boundary_with_reject_mode_blocks_the_punch(
        self, nominatim_cls
    ):
        self._mock_geocoder(nominatim_cls)
        GeoFencing.objects.create(
            tier=TIER_COMPANY, company=self.company,
            latitude=12.9716, longitude=77.5946, radius_in_meters=100, start=True,
            enforcement_mode=GeoFencing.ENFORCEMENT_REJECT,
        )
        # ~1200km away
        result = check_geo_fence(self.employee, 28.6139, 77.2090)
        self.assertFalse(result.allowed)
        self.assertTrue(result.violation)
        self.assertEqual(result.reason, "outside_boundary")

    def test_outside_the_boundary_with_flag_mode_allows_and_flags(self, nominatim_cls):
        self._mock_geocoder(nominatim_cls)
        GeoFencing.objects.create(
            tier=TIER_COMPANY, company=self.company,
            latitude=12.9716, longitude=77.5946, radius_in_meters=100, start=True,
            enforcement_mode=GeoFencing.ENFORCEMENT_FLAG,
        )
        result = check_geo_fence(self.employee, 28.6139, 77.2090)
        self.assertTrue(result.allowed)
        self.assertTrue(result.violation)
        self.assertFalse(result.unverified)

    def test_malformed_coordinates_are_unverified_not_a_violation(self, nominatim_cls):
        self._mock_geocoder(nominatim_cls)
        GeoFencing.objects.create(
            tier=TIER_COMPANY, company=self.company,
            latitude=12.9716, longitude=77.5946, radius_in_meters=100, start=True,
        )
        result = check_geo_fence(self.employee, "not-a-number", "also-not-a-number")
        self.assertTrue(result.allowed)
        self.assertFalse(result.violation)
        self.assertTrue(result.unverified)

    def test_employee_type_exemption_is_allowed_regardless_of_distance(
        self, nominatim_cls
    ):
        self._mock_geocoder(nominatim_cls)
        GeoFencing.objects.create(
            tier=TIER_COMPANY, company=self.company,
            latitude=12.9716, longitude=77.5946, radius_in_meters=100, start=True,
            enforcement_mode=GeoFencing.ENFORCEMENT_REJECT,
        )
        emp_type = EmployeeType.objects.create(
            employee_type="Contract", collar_category=COLLAR_WHITE
        )
        emp_type.company_id.add(self.company)
        from employee.models import EmployeeWorkInformation

        EmployeeWorkInformation.objects.filter(employee_id=self.employee).update(
            employee_type_id=emp_type
        )
        GeoFencing.objects.create(
            tier=TIER_EMPLOYEE_TYPE, company=self.company,
            employee_type_category=COLLAR_WHITE, start=True,
        )
        from employee.models import Employee

        employee = Employee.objects.get(pk=self.employee.pk)
        result = check_geo_fence(employee, 28.6139, 77.2090)  # far away
        self.assertTrue(result.allowed)
        self.assertFalse(result.violation)

    def test_department_boundary_still_enforces_even_for_an_exempt_type(
        self, nominatim_cls
    ):
        self._mock_geocoder(nominatim_cls)
        emp_type = EmployeeType.objects.create(
            employee_type="Contract", collar_category=COLLAR_WHITE
        )
        emp_type.company_id.add(self.company)
        GeoFencing.objects.create(
            tier=TIER_EMPLOYEE_TYPE, company=self.company,
            employee_type_category=COLLAR_WHITE, start=True,
        )
        dept = Department.objects.create(department="Warehouse")
        dept.company_id.add(self.company)
        GeoFencing.objects.create(
            tier=TIER_DEPARTMENT, company=self.company, department=dept,
            latitude=12.9716, longitude=77.5946, radius_in_meters=100, start=True,
            enforcement_mode=GeoFencing.ENFORCEMENT_REJECT,
        )
        from employee.models import Employee, EmployeeWorkInformation

        EmployeeWorkInformation.objects.filter(employee_id=self.employee).update(
            employee_type_id=emp_type, department_id=dept,
        )
        employee = Employee.objects.get(pk=self.employee.pk)
        result = check_geo_fence(employee, 28.6139, 77.2090)  # far away
        self.assertFalse(result.allowed)
        self.assertTrue(result.violation)
