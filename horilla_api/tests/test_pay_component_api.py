"""Earnings / deductions APIs: create, update, publish, deactivate."""

from django.contrib.auth.models import Permission
from django.test import TestCase
from rest_framework.test import APIClient

from horilla.horilla_middlewares import _thread_locals, set_selected_company
from horilla.testkit import make_company, make_employee, make_user
from krew_payroll.methods.pay_components import rules

BASE = "/api/payroll"


class PayComponentAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.company = make_company("Sarvam API")
        self.user = make_user("payroll_api", password="secret123")
        make_employee(company=self.company, email="payroll_api@test.horilla", user=self.user)
        perms = Permission.objects.filter(
            content_type__app_label="krew_payroll",
            codename__in=["add_paycomponent", "change_paycomponent"],
        )
        self.user.user_permissions.add(*perms)
        # Reload so the user's employee / work-info company isn't a stale cache.
        self.user = type(self.user).objects.get(pk=self.user.pk)
        self.client.force_authenticate(user=self.user)
        self.start = rules.today().isoformat()

    def tearDown(self):
        set_selected_company(None)
        _thread_locals.request = None
        super().tearDown()

    def create(self, route="earnings", name="Attendance Bonus"):
        return self.client.post(f"{BASE}/{route}/", {"name": name}, format="json")

    def test_create_returns_201_with_draft_v1(self):
        response = self.create()
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["type"], "EARNING")
        self.assertEqual(response.data["code"], "AB")
        self.assertEqual(response.data["version"]["publish_state"], "DRAFT")
        self.assertEqual(response.data["version"]["calculations"][0]["purpose"], "COMPONENT_VALUE")
        bad = self.client.post(f"{BASE}/earnings/", {"name": "X", "code": "Y"}, format="json")
        self.assertEqual(bad.status_code, 400)
        self.assertIn("code", bad.data["errors"])

    def test_update_publish_deactivate_flow(self):
        cid = self.create().data["id"]
        url = f"{BASE}/earnings/{cid}/versions/1/"
        response = self.client.patch(url, {"payslip_label": "Bonus"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(response.data["checks"]["ready_to_publish"])
        self.assertEqual(self.client.post(url + "publish/").status_code, 400)
        body = {
            "effective_from": self.start,
            "calculations": [{"purpose": "COMPONENT_VALUE", "mode": "FLAT", "flat_amount": 1500}],
        }
        response = self.client.patch(url, body, format="json")
        self.assertTrue(response.data["checks"]["ready_to_publish"], response.data)
        response = self.client.post(url + "publish/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["version"]["publish_state"], "PUBLISHED")
        self.assertEqual(self.client.post(url + "publish/").status_code, 409)
        response = self.client.patch(
            url,
            {"calculations": [{"purpose": "COMPONENT_VALUE", "mode": "FLAT", "flat_amount": 0}]},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        response = self.client.post(f"{BASE}/earnings/{cid}/deactivate/")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["is_active"])
        self.assertEqual(response.data["dependants"], [])

    def test_shape_errors(self):
        cid = self.create().data["id"]
        response = self.client.patch(
            f"{BASE}/earnings/{cid}/versions/1/",
            {"reduces_taxable_income": True, "effective_from": "2030-01-15"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["errors"]["reduces_taxable_income"], ["not a field of an earning"])
        self.assertNotIn("effective_from", response.data["errors"])  # any day can start a version

    def test_route_fixes_the_type(self):
        cid = self.create().data["id"]
        response = self.client.patch(f"{BASE}/deductions/{cid}/versions/1/", {"max_amount": 5}, format="json")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.client.post(f"{BASE}/deductions/{cid}/deactivate/").status_code, 404)
        self.assertEqual(self.create(route="deductions", name="Canteen").data["type"], "DEDUCTION")

    def test_permission_and_auth(self):
        other = make_user("no_perm_api", password="secret123")
        make_employee(company=self.company, email="no_perm_api@test.horilla", user=other)
        self.client.force_authenticate(user=type(other).objects.get(pk=other.pk))
        self.assertEqual(self.create().status_code, 403)
        self.client.force_authenticate(user=None)
        self.assertEqual(self.create().status_code, 401)

    def test_company_header_must_be_allowed(self):
        stranger = make_company("Not Mine")
        response = self.client.post(
            f"{BASE}/earnings/", {"name": "X Pay"}, format="json", HTTP_X_COMPANY_ID=str(stranger.id)
        )
        self.assertEqual(response.status_code, 400)
        response = self.client.post(
            f"{BASE}/earnings/", {"name": "X Pay"}, format="json", HTTP_X_COMPANY_ID=str(self.company.id)
        )
        self.assertEqual(response.status_code, 201)
