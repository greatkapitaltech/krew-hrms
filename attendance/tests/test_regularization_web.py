"""
Web-view tests for the new correction flow (#8 Regularization) --
attendance/cbv/regularization_request.py.
"""

from datetime import date, time

from django.test import TestCase

from attendance.models import Attendance, AttendanceRuleSet, RegularizationRequest
from employee.models import Employee, EmployeeWorkInformation
from horilla.horilla_middlewares import _thread_locals
from horilla.testkit.company import clear_selected_company
from horilla.testkit.factories import make_company, make_employee, make_user
from horilla_auth.models import HorillaUser


class RegularizationWebTestBase(TestCase):
    PAGE_URL = "/attendance/regularization-requests/"
    LIST_URL = "/attendance/regularization-requests/list/"
    FORM_URL = "/attendance/regularization-requests/form/"

    def setUp(self):
        # A prior test's real authenticated request (this file drives
        # some via self.client) leaves both _thread_locals.request and
        # the separate current_company_id ContextVar (see
        # horilla/horilla_middlewares.py) set from that request -- and
        # neither is reset between TestCase methods/classes. The first
        # poisons created_by on fixtures created here (see
        # horilla_api/tests/test_regularization_api.py); the second
        # poisons HorillaCompanyManager-scoped reads (e.g. Employee's
        # reverse employee_work_info lookup) with a company id that no
        # longer exists once that prior test's transaction rolled back --
        # make_employee()'s own re-fetch then raises DoesNotExist. Both
        # need clearing before any fixture is created.
        _thread_locals.request = None
        clear_selected_company()
        self.company = make_company("Regularization Web Co")
        self.addCleanup(self._clear_context_vars)
        AttendanceRuleSet.objects.create(
            tier="COMPANY",
            company=self.company,
            regularization_enabled=True,
            regularization_monthly_cap=2,
        )
        self.manager_user = make_user("reg_web_mgr", password="secret123")
        self.manager = make_employee(
            company=self.company,
            email="reg_web_mgr@test.horilla",
            user=self.manager_user,
        )
        self.employee_user = make_user("reg_web_emp", password="secret123")
        self.employee = make_employee(
            company=self.company,
            email="reg_web_emp@test.horilla",
            user=self.employee_user,
        )
        EmployeeWorkInformation.objects.filter(employee_id=self.employee).update(
            reporting_manager_id=self.manager
        )
        self.employee = Employee.objects.get(pk=self.employee.pk)

        self.manager_user = HorillaUser.objects.get(pk=self.manager_user.pk)
        self.employee_user = HorillaUser.objects.get(pk=self.employee_user.pk)

        self.attendance = Attendance.objects.create(
            employee_id=self.employee,
            attendance_date=date(2026, 9, 21),
            attendance_clock_in=time(9, 30),
            attendance_clock_in_date=date(2026, 9, 21),
            attendance_worked_hour="07:30",
        )

    @staticmethod
    def _clear_context_vars():
        # This test drives real requests through self.client, which sets
        # both ContextVars again during the test body itself (not just
        # setUp) -- so the next test to run anywhere in the process needs
        # this cleared afterward too, or it inherits our request's
        # company/user. addCleanup (not tearDown) still runs this even if
        # setUp itself fails partway through.
        _thread_locals.request = None
        clear_selected_company()


class RegularizationWebPageTests(RegularizationWebTestBase):
    def test_page_loads_for_a_logged_in_employee(self):
        self.client.force_login(self.employee_user)
        response = self.client.get(self.PAGE_URL)
        self.assertEqual(response.status_code, 200)

    def test_list_endpoint_shows_only_own_and_subordinate_requests(self):
        own_request = RegularizationRequest.objects.create(
            employee=self.employee,
            attendance=self.attendance,
            reason_code=RegularizationRequest.REASON_TIME_CORRECTION,
            reason="Late clock-in.",
            corrected_clock_in=time(9, 0),
        )
        other_user = make_user("reg_web_other", password="secret123")
        other_employee = make_employee(
            company=self.company,
            email="reg_web_other@test.horilla",
            user=other_user,
        )
        other_attendance = Attendance.objects.create(
            employee_id=other_employee,
            attendance_date=date(2026, 9, 21),
            attendance_clock_in=time(9, 30),
            attendance_clock_in_date=date(2026, 9, 21),
            attendance_worked_hour="07:30",
        )
        other_request = RegularizationRequest.objects.create(
            employee=other_employee,
            attendance=other_attendance,
            reason_code=RegularizationRequest.REASON_TIME_CORRECTION,
            reason="Unrelated request.",
            corrected_clock_in=time(9, 0),
        )

        self.client.force_login(self.employee_user)
        response = self.client.get(self.LIST_URL, HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn(str(own_request.pk), content)
        self.assertNotIn(f'data-object-id="{other_request.pk}"', content)

    def test_create_form_renders(self):
        self.client.force_login(self.employee_user)
        response = self.client.get(self.FORM_URL, HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 200)

    def test_create_form_submission_raises_a_request(self):
        self.client.force_login(self.employee_user)
        response = self.client.post(
            self.FORM_URL,
            {
                "attendance": self.attendance.pk,
                "reason_code": RegularizationRequest.REASON_TIME_CORRECTION,
                "reason": "Forgot to clock in on time.",
                "corrected_clock_in": "09:00",
            },
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            RegularizationRequest.objects.filter(employee=self.employee).exists()
        )

    def test_auto_close_dispute_is_not_a_selectable_choice(self):
        self.client.force_login(self.employee_user)
        response = self.client.get(self.FORM_URL, HTTP_HX_REQUEST="true")
        self.assertNotIn(
            RegularizationRequest.REASON_AUTO_CLOSE_DISPUTE,
            response.content.decode(),
        )


class RegularizationWebApproveRejectTests(RegularizationWebTestBase):
    def setUp(self):
        super().setUp()
        self.reg_request = RegularizationRequest.objects.create(
            employee=self.employee,
            attendance=self.attendance,
            reason_code=RegularizationRequest.REASON_TIME_CORRECTION,
            reason="Late clock-in due to traffic.",
            corrected_clock_in=time(9, 0),
        )
        self.approve_url = f"/attendance/regularization-requests/{self.reg_request.pk}/approve/"
        self.reject_url = f"/attendance/regularization-requests/{self.reg_request.pk}/reject/"

    def test_reporting_manager_can_approve(self):
        self.client.force_login(self.manager_user)
        response = self.client.post(self.approve_url)
        self.assertEqual(response.status_code, 200)
        self.reg_request.refresh_from_db()
        self.assertEqual(self.reg_request.status, RegularizationRequest.STATUS_APPROVED)

    def test_reporting_manager_can_reject(self):
        self.client.force_login(self.manager_user)
        response = self.client.post(self.reject_url)
        self.assertEqual(response.status_code, 200)
        self.reg_request.refresh_from_db()
        self.assertEqual(self.reg_request.status, RegularizationRequest.STATUS_REJECTED)

    def test_employee_cannot_approve_their_own_request(self):
        self.client.force_login(self.employee_user)
        self.client.post(self.approve_url)
        self.reg_request.refresh_from_db()
        self.assertEqual(self.reg_request.status, RegularizationRequest.STATUS_PENDING)

    def test_get_is_not_allowed(self):
        self.client.force_login(self.manager_user)
        response = self.client.get(self.approve_url)
        self.assertEqual(response.status_code, 405)
