"""
Web-view tests for the GeoFencing three-tier settings screen
(geofencing/cbv/geo_fencing_rule.py) -- exposing the tiers
geo_location_config() (the pre-existing single-row, Company-Default-only
form embedded in the "Attendance Rules" settings page) can't reach.
"""

from unittest.mock import MagicMock, patch

from django.test import TestCase

from base.config_tiers import TIER_COMPANY, TIER_DEPARTMENT, TIER_EMPLOYEE_TYPE
from base.models import Department
from geofencing.models import GeoFencing
from horilla.horilla_middlewares import _thread_locals
from horilla.testkit.company import clear_selected_company
from horilla.testkit.factories import make_company, make_employee, make_user


def make_department(name, *, company):
    department = Department.objects.create(department=name)
    department.company_id.add(company)
    return department


class GeoFencingRuleWebTestBase(TestCase):
    PAGE_URL = "/api/geofencing/rules/"
    LIST_URL = "/api/geofencing/rules/list/"
    CREATE_URL = "/api/geofencing/rules/create/"
    HX = {"HTTP_HX_REQUEST": "true"}

    def update_url(self, pk):
        return f"/api/geofencing/rules/{pk}/update/"

    def setUp(self):
        _thread_locals.request = None
        clear_selected_company()
        self.company = make_company("Geo Rule Web Co")
        self.addCleanup(self._clear_context_vars)

        self.admin_user = make_user("georule_web_admin", is_superuser=True)
        self.admin = make_employee(
            company=self.company,
            email="georule_web_admin@test.horilla",
            user=self.admin_user,
        )
        self.client.force_login(self.admin_user)
        session = self.client.session
        session["selected_company"] = str(self.company.pk)
        session.save()

    def _clear_context_vars(self):
        _thread_locals.request = None
        clear_selected_company()

    @staticmethod
    def _mock_geocoder(nominatim_cls):
        nominatim_cls.return_value.reverse.return_value = MagicMock()


@patch("geofencing.models.Nominatim")
class PageAndListTests(GeoFencingRuleWebTestBase):
    def test_page_renders_for_a_selected_company(self, nominatim_cls):
        response = self.client.get(self.PAGE_URL)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Select a single company")

    def test_page_shows_a_prompt_when_no_single_company_is_selected(self, nominatim_cls):
        session = self.client.session
        session["selected_company"] = "all"
        session.save()
        response = self.client.get(self.PAGE_URL)
        self.assertContains(response, "Select a single company")

    def test_list_renders_with_zero_rows(self, nominatim_cls):
        response = self.client.get(self.LIST_URL, **self.HX)
        self.assertEqual(response.status_code, 200)


@patch("geofencing.models.Nominatim")
class CreateCompanyDefaultTests(GeoFencingRuleWebTestBase):
    def test_create_saves_immediately_no_pending_change(self, nominatim_cls):
        self._mock_geocoder(nominatim_cls)
        response = self.client.post(
            self.CREATE_URL,
            data={
                "tier": TIER_COMPANY,
                "latitude": "12.9716",
                "longitude": "77.5946",
                "radius_in_meters": "200",
                "enforcement_mode": "REJECT",
            },
            **self.HX,
        )
        self.assertEqual(response.status_code, 200)
        row = GeoFencing.objects.get(company=self.company, tier=TIER_COMPANY)
        # Direct save, unlike AttendanceRuleSet -- no PendingConfigChange
        # involved, the row reflects the submitted values right away.
        self.assertEqual(row.latitude, 12.9716)
        self.assertEqual(row.radius_in_meters, 200)

    def test_a_second_company_default_is_rejected(self, nominatim_cls):
        self._mock_geocoder(nominatim_cls)
        self.client.post(
            self.CREATE_URL,
            data={
                "tier": TIER_COMPANY, "latitude": "1", "longitude": "1",
                "radius_in_meters": "100", "enforcement_mode": "REJECT",
            },
            **self.HX,
        )
        response = self.client.post(
            self.CREATE_URL,
            data={
                "tier": TIER_COMPANY, "latitude": "2", "longitude": "2",
                "radius_in_meters": "50", "enforcement_mode": "REJECT",
            },
            **self.HX,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            GeoFencing.objects.filter(company=self.company, tier=TIER_COMPANY).count(),
            1,
        )


@patch("geofencing.models.Nominatim")
class EmployeeTypeTierExcludedTests(GeoFencingRuleWebTestBase):
    """
    This screen is deliberately scoped to Company Default and Department
    Override only (see GeoFencingRuleForm's own docstring) -- confirms
    that scoping is real, not just cosmetic: a tampered POST claiming
    tier=EMPLOYEE_TYPE can't create a row, since it's not a valid choice
    on this form at all.
    """

    def test_employee_type_tier_is_rejected_as_an_invalid_choice(self, nominatim_cls):
        response = self.client.post(
            self.CREATE_URL,
            data={
                "tier": TIER_EMPLOYEE_TYPE,
                "latitude": "5", "longitude": "5", "radius_in_meters": "50",
                "enforcement_mode": "REJECT",
            },
            **self.HX,
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            GeoFencing.objects.filter(
                company=self.company, tier=TIER_EMPLOYEE_TYPE
            ).exists()
        )
        # No row of ANY tier either -- the whole form is invalid (tier
        # itself fails as an unrecognized choice), not just silently
        # coerced to a different tier.
        self.assertFalse(GeoFencing.objects.filter(company=self.company).exists())


@patch("geofencing.models.Nominatim")
class DepartmentOverrideTests(GeoFencingRuleWebTestBase):
    def test_a_department_not_linked_to_this_company_is_rejected(self, nominatim_cls):
        self._mock_geocoder(nominatim_cls)
        other_company = make_company("Some Other Geo Co")
        foreign_department = make_department("Foreign Dept", company=other_company)

        response = self.client.post(
            self.CREATE_URL,
            data={
                "tier": TIER_DEPARTMENT, "department": foreign_department.pk,
                "latitude": "1", "longitude": "1", "radius_in_meters": "100",
                "enforcement_mode": "REJECT",
            },
            **self.HX,
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            GeoFencing.objects.filter(
                company=self.company, tier=TIER_DEPARTMENT
            ).exists()
        )

    def test_a_department_override_saves_its_own_boundary(self, nominatim_cls):
        self._mock_geocoder(nominatim_cls)
        department = make_department("Ops", company=self.company)
        response = self.client.post(
            self.CREATE_URL,
            data={
                "tier": TIER_DEPARTMENT, "department": department.pk,
                "latitude": "10", "longitude": "20", "radius_in_meters": "300",
                "enforcement_mode": "FLAG",
            },
            **self.HX,
        )
        self.assertEqual(response.status_code, 200)
        row = GeoFencing.objects.get(company=self.company, tier=TIER_DEPARTMENT)
        self.assertEqual(row.department_id, department.pk)
        self.assertEqual(row.enforcement_mode, "FLAG")


@patch("geofencing.models.Nominatim")
class EditTests(GeoFencingRuleWebTestBase):
    def test_editing_updates_the_boundary_immediately(self, nominatim_cls):
        self._mock_geocoder(nominatim_cls)
        self.client.post(
            self.CREATE_URL,
            data={
                "tier": TIER_COMPANY, "latitude": "1", "longitude": "1",
                "radius_in_meters": "100", "enforcement_mode": "REJECT",
            },
            **self.HX,
        )
        row = GeoFencing.objects.get(company=self.company, tier=TIER_COMPANY)

        response = self.client.post(
            self.update_url(row.pk),
            data={
                "tier": TIER_COMPANY, "latitude": "9", "longitude": "9",
                "radius_in_meters": "500", "enforcement_mode": "FLAG",
            },
            **self.HX,
        )
        self.assertEqual(response.status_code, 200)
        row.refresh_from_db()
        self.assertEqual(row.latitude, 9.0)
        self.assertEqual(row.radius_in_meters, 500)
        self.assertEqual(row.enforcement_mode, "FLAG")

    def test_scope_fields_are_ignored_on_edit_even_if_tampered(self, nominatim_cls):
        self._mock_geocoder(nominatim_cls)
        self.client.post(
            self.CREATE_URL,
            data={
                "tier": TIER_COMPANY, "latitude": "1", "longitude": "1",
                "radius_in_meters": "100", "enforcement_mode": "REJECT",
            },
            **self.HX,
        )
        row = GeoFencing.objects.get(company=self.company, tier=TIER_COMPANY)
        department = make_department("Some Dept", company=self.company)

        self.client.post(
            self.update_url(row.pk),
            data={
                "tier": TIER_DEPARTMENT, "department": department.pk,
                "latitude": "9", "longitude": "9", "radius_in_meters": "500",
                "enforcement_mode": "REJECT",
            },
            **self.HX,
        )
        row.refresh_from_db()
        self.assertEqual(row.tier, TIER_COMPANY)
        self.assertIsNone(row.department_id)
