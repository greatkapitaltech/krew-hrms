"""
Tests for GeoFencing's tiered structure (base/config_tiers.py applied to
geofencing -- see geofencing/models.py's class docstring for the one
deliberate asymmetry: the Employee-Type tier here is a flat exemption,
not an alternate boundary).
"""

from unittest.mock import MagicMock, patch

from django.core.exceptions import ValidationError
from django.test import TestCase

from base.config_tiers import TIER_COMPANY, TIER_DEPARTMENT, TIER_EMPLOYEE_TYPE
from base.models import COLLAR_BLUE, COLLAR_WHITE, Department, EmployeeType
from employee.models import EmployeeWorkInformation
from geofencing.models import GeoFencing
from horilla.testkit.factories import make_company, make_employee, make_user


@patch("geofencing.models.Nominatim")
class GeoFencingResolutionTests(TestCase):
    def setUp(self):
        self.company = make_company("Acme")
        self.dept = Department.objects.create(department="Warehouse")
        self.dept.company_id.add(self.company)
        self.emp_type = EmployeeType.objects.create(
            employee_type="Contract", collar_category=COLLAR_WHITE
        )
        self.emp_type.company_id.add(self.company)
        self.employee = make_employee(
            company=self.company, email="geo1@test.horilla", user=make_user("geo1"),
            department=self.dept,
        )

    def _mock_geocoder(self, nominatim_cls):
        nominatim_cls.return_value.reverse.return_value = MagicMock()

    def test_falls_back_to_company_default(self, nominatim_cls):
        self._mock_geocoder(nominatim_cls)
        company_default = GeoFencing.objects.create(
            tier=TIER_COMPANY, company=self.company,
            latitude=1.0, longitude=1.0, radius_in_meters=100,
        )
        resolved = GeoFencing.resolve_for_employee(self.employee)
        self.assertEqual(resolved, company_default)

    def test_employee_type_exemption_wins_over_company_default(self, nominatim_cls):
        self._mock_geocoder(nominatim_cls)
        GeoFencing.objects.create(
            tier=TIER_COMPANY, company=self.company,
            latitude=1.0, longitude=1.0, radius_in_meters=100,
        )
        exemption = GeoFencing.objects.create(
            tier=TIER_EMPLOYEE_TYPE, company=self.company,
            employee_type_category=COLLAR_WHITE,
        )
        EmployeeWorkInformation.objects.filter(employee_id=self.employee).update(
            department_id=None, employee_type_id=self.emp_type,
        )
        from employee.models import Employee
        employee = Employee.objects.get(pk=self.employee.pk)
        resolved = GeoFencing.resolve_for_employee(employee)
        self.assertEqual(resolved, exemption)
        self.assertTrue(resolved.is_exemption())

    def test_department_boundary_wins_over_employee_type_exemption(self, nominatim_cls):
        self._mock_geocoder(nominatim_cls)
        GeoFencing.objects.create(
            tier=TIER_COMPANY, company=self.company,
            latitude=1.0, longitude=1.0, radius_in_meters=100,
        )
        GeoFencing.objects.create(
            tier=TIER_EMPLOYEE_TYPE, company=self.company,
            employee_type_category=COLLAR_WHITE,
        )
        dept_boundary = GeoFencing.objects.create(
            tier=TIER_DEPARTMENT, company=self.company, department=self.dept,
            latitude=2.0, longitude=2.0, radius_in_meters=50,
        )
        resolved = GeoFencing.resolve_for_employee(self.employee)
        self.assertEqual(resolved, dept_boundary)
        self.assertFalse(resolved.is_exemption())


@patch("geofencing.models.Nominatim")
class GeoFencingCleanValidationTests(TestCase):
    def setUp(self):
        self.company = make_company("Acme")

    def _mock_geocoder(self, nominatim_cls):
        nominatim_cls.return_value.reverse.return_value = MagicMock()

    def test_company_default_requires_boundary_fields(self, nominatim_cls):
        self._mock_geocoder(nominatim_cls)
        instance = GeoFencing(tier=TIER_COMPANY, company=self.company)
        with self.assertRaises(ValidationError):
            instance.full_clean()

    def test_employee_type_exemption_rejects_boundary_fields(self, nominatim_cls):
        self._mock_geocoder(nominatim_cls)
        emp_type = EmployeeType.objects.create(
            employee_type="Contract", collar_category=COLLAR_WHITE
        )
        emp_type.company_id.add(self.company)
        instance = GeoFencing(
            tier=TIER_EMPLOYEE_TYPE, company=self.company,
            employee_type_category=COLLAR_WHITE,
            latitude=1.0, longitude=1.0, radius_in_meters=50,
        )
        with self.assertRaises(ValidationError):
            instance.full_clean()

    def test_employee_type_exemption_with_blank_boundary_is_valid(self, nominatim_cls):
        self._mock_geocoder(nominatim_cls)
        emp_type = EmployeeType.objects.create(
            employee_type="Contract", collar_category=COLLAR_BLUE
        )
        emp_type.company_id.add(self.company)
        instance = GeoFencing(
            tier=TIER_EMPLOYEE_TYPE, company=self.company,
            employee_type_category=COLLAR_BLUE,
        )
        instance.full_clean()  # must not raise

    def test_department_boundary_requires_department_belong_to_company(
        self, nominatim_cls
    ):
        self._mock_geocoder(nominatim_cls)
        other_company = make_company("Globex")
        dept = Department.objects.create(department="Warehouse")
        dept.company_id.add(other_company)
        instance = GeoFencing(
            tier=TIER_DEPARTMENT, company=self.company, department=dept,
            latitude=1.0, longitude=1.0, radius_in_meters=50,
        )
        with self.assertRaises(ValidationError):
            instance.full_clean()

    def test_second_company_default_rejected(self, nominatim_cls):
        self._mock_geocoder(nominatim_cls)
        GeoFencing.objects.create(
            tier=TIER_COMPANY, company=self.company,
            latitude=1.0, longitude=1.0, radius_in_meters=100,
        )
        with self.assertRaises(Exception):
            GeoFencing.objects.create(
                tier=TIER_COMPANY, company=self.company,
                latitude=2.0, longitude=2.0, radius_in_meters=100,
            )
