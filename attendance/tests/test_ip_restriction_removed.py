"""
Regression test for Part 3 Sec 3.1 of the Attendance performance plan --
IP-based restriction was removed from clock_in()/clock_out() entirely
(attendance/views/clock_in_out.py). AttendanceAllowedIP itself, and its
settings screen, stay -- only the enforcement is gone.
"""

from django.test import TestCase

from base.models import AttendanceAllowedIP
from horilla.horilla_middlewares import _thread_locals
from horilla.testkit.company import clear_selected_company
from horilla.testkit.factories import make_company, make_employee, make_user
from horilla_auth.models import HorillaUser


class IPRestrictionNoLongerEnforcedTests(TestCase):
    def setUp(self):
        _thread_locals.request = None
        clear_selected_company()
        self.addCleanup(self._clear_context_vars)
        self.company = make_company("IP Restriction Co")
        user = make_user("ip_restrict_emp", password="secret123")
        make_employee(company=self.company, email="ip_restrict_emp@test.horilla", user=user)
        self.user = HorillaUser.objects.get(pk=user.pk)

        # Configured to only allow a network the test client's request
        # will never actually originate from -- under the old behavior
        # this would have blocked the clock-in with a 302 + error message.
        AttendanceAllowedIP.objects.create(
            company_id=self.company,
            is_enabled=True,
            additional_data={"allowed_ips": ["10.0.0.0/24"]},
        )

    @staticmethod
    def _clear_context_vars():
        _thread_locals.request = None
        clear_selected_company()

    def test_clock_in_succeeds_from_a_disallowed_ip(self):
        self.client.force_login(self.user)
        session = self.client.session
        session["selected_company"] = str(self.company.id)
        session.save()

        response = self.client.get(
            "/attendance/clock-in/",
            HTTP_HX_REQUEST="true",
            REMOTE_ADDR="203.0.113.5",  # not in the configured allowed_ips
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"not authorized", response.content)
