"""
REST API tests for the new Regularization endpoint (#8) --
RegularizationRequestView / Approve / Reject in
horilla_api/api_views/attendance/views.py.
"""

from datetime import date, time

from django.test import TestCase
from rest_framework.test import APIClient

from attendance.models import Attendance, AttendanceRuleSet, RegularizationRequest
from employee.models import Employee, EmployeeWorkInformation
from horilla.horilla_middlewares import _thread_locals
from horilla.testkit.company import clear_selected_company
from horilla.testkit.factories import make_company, make_employee, make_user
from horilla_auth.models import HorillaUser


class RegularizationAPITestBase(TestCase):
    URL_LIST = "/api/attendance/regularization-request/"

    def setUp(self):
        # A prior test's last authenticated API call leaves its user
        # cached on this ContextVar; TestCase's transaction rollback
        # deletes that row from the DB but not the in-memory reference,
        # so HorillaModel.save() (see horilla/models.py) would otherwise
        # stamp fixtures created below with a now-nonexistent created_by,
        # violating the FK. Clearing it makes each test's fixture setup
        # independent of whatever the previous test last authenticated as.
        _thread_locals.request = None
        clear_selected_company()
        self.addCleanup(self._clear_context_vars)
        self.client = APIClient()
        self.company = make_company("Regularization API Co")
        AttendanceRuleSet.objects.create(
            tier="COMPANY",
            company=self.company,
            regularization_enabled=True,
            regularization_monthly_cap=2,
        )
        self.manager_user = make_user("reg_api_mgr", password="secret123")
        self.manager = make_employee(
            company=self.company,
            email="reg_api_mgr@test.horilla",
            user=self.manager_user,
        )
        self.employee_user = make_user("reg_api_emp", password="secret123")
        self.employee = make_employee(
            company=self.company,
            email="reg_api_emp@test.horilla",
            user=self.employee_user,
        )
        EmployeeWorkInformation.objects.filter(employee_id=self.employee).update(
            reporting_manager_id=self.manager
        )
        self.employee = Employee.objects.get(pk=self.employee.pk)

        self.other_user = make_user("reg_api_other", password="secret123")
        self.other_employee = make_employee(
            company=self.company,
            email="reg_api_other@test.horilla",
            user=self.other_user,
        )

        # Employee.save() checks `hasattr(self, "employee_work_info")` to
        # decide whether to auto-create one (employee/models.py) -- before
        # that row exists, Django's reverse-o2o descriptor permanently
        # caches the "doesn't exist yet" result on *this* Employee
        # instance. Assigning employee_user_id=user just before save()
        # (as make_employee() does) also back-fills that same Employee
        # instance onto user's own reverse cache (user.employee_get), so
        # authenticating as the *original* user object served that
        # forever-stale, company-less Employee to every view in this
        # test. Re-fetching each user here sidesteps the stale cache
        # entirely -- a fresh HorillaUser has no cache to poison.
        self.manager_user = HorillaUser.objects.get(pk=self.manager_user.pk)
        self.employee_user = HorillaUser.objects.get(pk=self.employee_user.pk)
        self.other_user = HorillaUser.objects.get(pk=self.other_user.pk)

        self.attendance = Attendance.objects.create(
            employee_id=self.employee,
            attendance_date=date(2026, 9, 21),
            attendance_clock_in=time(9, 30),
            attendance_clock_in_date=date(2026, 9, 21),
            attendance_worked_hour="07:30",
        )

    @staticmethod
    def _clear_context_vars():
        # force_authenticate + self.client requests set both ContextVars
        # again during the test body itself, not just setUp -- so the
        # next test anywhere in the process needs this cleared after too.
        # addCleanup (not tearDown) still runs even if setUp fails partway.
        _thread_locals.request = None
        clear_selected_company()


class RegularizationCreateAPITests(RegularizationAPITestBase):
    def test_employee_can_raise_a_time_correction_request(self):
        self.client.force_authenticate(user=self.employee_user)
        response = self.client.post(
            self.URL_LIST,
            {
                "attendance": self.attendance.pk,
                "reason_code": RegularizationRequest.REASON_TIME_CORRECTION,
                "reason": "Forgot to clock in on time.",
                "corrected_clock_in": "09:00",
            },
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["status"], RegularizationRequest.STATUS_PENDING)
        self.assertEqual(response.data["employee"], self.employee.pk)
        self.assertTrue(
            RegularizationRequest.objects.filter(employee=self.employee).exists()
        )

    def test_auto_close_dispute_is_rejected_at_the_api_layer(self):
        self.client.force_authenticate(user=self.employee_user)
        response = self.client.post(
            self.URL_LIST,
            {
                "attendance": self.attendance.pk,
                "reason_code": RegularizationRequest.REASON_AUTO_CLOSE_DISPUTE,
                "reason": "Auto punch-out was wrong.",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("reason_code", response.data)

    def test_disabled_regularization_is_rejected(self):
        AttendanceRuleSet.objects.all().update(regularization_enabled=False)
        self.client.force_authenticate(user=self.employee_user)
        response = self.client.post(
            self.URL_LIST,
            {
                "attendance": self.attendance.pk,
                "reason_code": RegularizationRequest.REASON_TIME_CORRECTION,
                "reason": "Forgot to clock in on time.",
                "corrected_clock_in": "09:00",
            },
        )
        self.assertEqual(response.status_code, 400)

    def test_monthly_cap_is_enforced(self):
        self.client.force_authenticate(user=self.employee_user)
        for _ in range(2):
            response = self.client.post(
                self.URL_LIST,
                {
                    "attendance": self.attendance.pk,
                    "reason_code": RegularizationRequest.REASON_TIME_CORRECTION,
                    "reason": "Correction.",
                    "corrected_clock_in": "09:00",
                },
            )
            self.assertEqual(response.status_code, 201)
        response = self.client.post(
            self.URL_LIST,
            {
                "attendance": self.attendance.pk,
                "reason_code": RegularizationRequest.REASON_TIME_CORRECTION,
                "reason": "One too many.",
                "corrected_clock_in": "09:00",
            },
        )
        self.assertEqual(response.status_code, 400)

    def test_unauthenticated_is_rejected(self):
        response = self.client.post(
            self.URL_LIST,
            {
                "attendance": self.attendance.pk,
                "reason_code": RegularizationRequest.REASON_TIME_CORRECTION,
                "reason": "Correction.",
                "corrected_clock_in": "09:00",
            },
        )
        self.assertEqual(response.status_code, 401)


class RegularizationListAPITests(RegularizationAPITestBase):
    def setUp(self):
        super().setUp()
        self.own_request = RegularizationRequest.objects.create(
            employee=self.employee,
            attendance=self.attendance,
            reason_code=RegularizationRequest.REASON_TIME_CORRECTION,
            reason="Late clock-in.",
            corrected_clock_in=time(9, 0),
        )
        other_attendance = Attendance.objects.create(
            employee_id=self.other_employee,
            attendance_date=date(2026, 9, 21),
            attendance_clock_in=time(9, 30),
            attendance_clock_in_date=date(2026, 9, 21),
            attendance_worked_hour="07:30",
        )
        self.other_request = RegularizationRequest.objects.create(
            employee=self.other_employee,
            attendance=other_attendance,
            reason_code=RegularizationRequest.REASON_TIME_CORRECTION,
            reason="Unrelated request.",
            corrected_clock_in=time(9, 0),
        )

    def test_employee_sees_only_their_own_requests(self):
        self.client.force_authenticate(user=self.employee_user)
        response = self.client.get(self.URL_LIST)
        self.assertEqual(response.status_code, 200)
        ids = [row["id"] for row in response.data["results"]]
        self.assertIn(self.own_request.pk, ids)
        self.assertNotIn(self.other_request.pk, ids)

    def test_manager_sees_subordinates_requests(self):
        self.client.force_authenticate(user=self.manager_user)
        response = self.client.get(self.URL_LIST)
        self.assertEqual(response.status_code, 200)
        ids = [row["id"] for row in response.data["results"]]
        self.assertIn(self.own_request.pk, ids)
        self.assertNotIn(self.other_request.pk, ids)


class RegularizationApproveRejectAPITests(RegularizationAPITestBase):
    def setUp(self):
        super().setUp()
        self.reg_request = RegularizationRequest.objects.create(
            employee=self.employee,
            attendance=self.attendance,
            reason_code=RegularizationRequest.REASON_TIME_CORRECTION,
            reason="Late clock-in due to traffic.",
            corrected_clock_in=time(9, 0),
        )
        self.approve_url = f"/api/attendance/regularization-request-approve/{self.reg_request.pk}"
        self.reject_url = f"/api/attendance/regularization-request-reject/{self.reg_request.pk}"

    def test_reporting_manager_can_approve(self):
        self.client.force_authenticate(user=self.manager_user)
        response = self.client.put(self.approve_url, {"resolution_note": "Confirmed."})
        self.assertEqual(response.status_code, 200)
        self.reg_request.refresh_from_db()
        self.assertEqual(self.reg_request.status, RegularizationRequest.STATUS_APPROVED)
        self.attendance.refresh_from_db()
        self.assertEqual(str(self.attendance.attendance_clock_in), "09:00:00")

    def test_reporting_manager_can_reject(self):
        self.client.force_authenticate(user=self.manager_user)
        response = self.client.put(self.reject_url, {"resolution_note": "Not valid."})
        self.assertEqual(response.status_code, 200)
        self.reg_request.refresh_from_db()
        self.assertEqual(self.reg_request.status, RegularizationRequest.STATUS_REJECTED)

    def test_unrelated_employee_cannot_approve(self):
        self.client.force_authenticate(user=self.other_user)
        response = self.client.put(self.approve_url, {})
        self.assertEqual(response.status_code, 403)
        self.reg_request.refresh_from_db()
        self.assertEqual(self.reg_request.status, RegularizationRequest.STATUS_PENDING)

    def test_employee_cannot_approve_their_own_request(self):
        self.client.force_authenticate(user=self.employee_user)
        response = self.client.put(self.approve_url, {})
        self.assertEqual(response.status_code, 403)

    def test_double_resolution_is_rejected(self):
        self.client.force_authenticate(user=self.manager_user)
        first = self.client.put(self.approve_url, {})
        self.assertEqual(first.status_code, 200)
        second = self.client.put(self.approve_url, {})
        self.assertEqual(second.status_code, 400)
