"""
Web-view tests for the eligible-days list on the Regularization screen
(RegularizableAttendanceListView, attendance/cbv/regularization_request.py)
-- attendance days actually flagged (geo violation/unverified, or
overtime awaiting approval) that a Regularization request can address,
plus the "Raise Request" row action pre-filling and locking the form to
that specific day/reason.
"""

from datetime import date, time, timedelta

from django.test import TestCase

from attendance.models import Attendance, AttendanceRuleSet, RegularizationRequest
from employee.models import Employee, EmployeeWorkInformation
from horilla.horilla_middlewares import _thread_locals
from horilla.testkit.company import clear_selected_company
from horilla.testkit.factories import make_company, make_employee, make_user
from horilla_auth.models import HorillaUser


class RegularizableAttendanceTestBase(TestCase):
    LIST_URL = "/attendance/regularization-requests/eligible/list/"
    FORM_URL = "/attendance/regularization-requests/form/"
    HX = {"HTTP_HX_REQUEST": "true"}

    def setUp(self):
        _thread_locals.request = None
        clear_selected_company()
        self.company = make_company("Regularizable Web Co")
        self.addCleanup(self._clear_context_vars)
        AttendanceRuleSet.objects.create(
            tier="COMPANY",
            company=self.company,
            regularization_enabled=True,
            regularization_monthly_cap=5,
        )
        self.employee_user = make_user("regel_emp", password="secret123")
        self.employee = make_employee(
            company=self.company,
            email="regel_emp@test.horilla",
            user=self.employee_user,
        )
        self.employee = Employee.objects.get(pk=self.employee.pk)
        self.employee_user = HorillaUser.objects.get(pk=self.employee_user.pk)
        self.client.force_login(self.employee_user)

    @staticmethod
    def _clear_context_vars():
        _thread_locals.request = None
        clear_selected_company()

    def _make_attendance(self, day_offset=0, **overrides):
        # overtime_second/attendance_overtime_approve/attendance_validated
        # can't be set through .create() -- Attendance.save() always
        # recomputes them from attendance_rule_set_snapshot (empty here,
        # no rule set configured for this fixture), so any values passed
        # for those three get silently overwritten back to 0/False/True.
        # Set post-creation via a raw .update() instead, which bypasses
        # save() entirely -- fine here since this file is only testing
        # the eligible-days query/display, not the overtime pipeline
        # itself (already covered by test_overtime_calculation.py).
        overtime_fields = {}
        for name in ("overtime_second", "attendance_overtime_approve", "attendance_validated"):
            if name in overrides:
                overtime_fields[name] = overrides.pop(name)

        defaults = dict(
            employee_id=self.employee,
            attendance_date=date(2026, 9, 21) - timedelta(days=day_offset),
            attendance_clock_in=time(9, 30),
            attendance_clock_in_date=date(2026, 9, 21),
            attendance_worked_hour="07:30",
        )
        defaults.update(overrides)
        attendance = Attendance.objects.create(**defaults)
        if overtime_fields:
            Attendance.objects.filter(pk=attendance.pk).update(**overtime_fields)
            attendance.refresh_from_db()
        return attendance


class EligibilityFilterTests(RegularizableAttendanceTestBase):
    def test_geo_violation_day_is_listed(self):
        attendance = self._make_attendance(geo_fence_violation=True)
        response = self.client.get(self.LIST_URL, **self.HX)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, str(attendance.attendance_date))

    def test_geo_unverified_day_is_listed(self):
        attendance = self._make_attendance(geo_fence_unverified=True)
        response = self.client.get(self.LIST_URL, **self.HX)
        self.assertContains(response, str(attendance.attendance_date))

    def test_overtime_awaiting_approval_is_listed(self):
        attendance = self._make_attendance(
            overtime_second=3600, attendance_overtime_approve=False,
            attendance_validated=False,
        )
        response = self.client.get(self.LIST_URL, **self.HX)
        self.assertContains(response, str(attendance.attendance_date))

    def test_overtime_already_approved_is_not_listed(self):
        attendance = self._make_attendance(
            overtime_second=3600, attendance_overtime_approve=True,
            attendance_validated=True,
        )
        response = self.client.get(self.LIST_URL, **self.HX)
        self.assertNotContains(response, str(attendance.attendance_date))

    def test_plain_unflagged_day_is_not_listed(self):
        attendance = self._make_attendance()
        response = self.client.get(self.LIST_URL, **self.HX)
        self.assertNotContains(response, str(attendance.attendance_date))

    def test_a_day_with_only_a_pending_dispute_already_raised_drops_off(self):
        attendance = self._make_attendance(geo_fence_violation=True)
        RegularizationRequest.objects.create(
            employee=self.employee, attendance=attendance,
            reason_code=RegularizationRequest.REASON_GEO_VIOLATION_DISPUTE,
            reason="Already disputed.",
        )
        response = self.client.get(self.LIST_URL, **self.HX)
        self.assertNotContains(response, str(attendance.attendance_date))

    def test_a_rejected_dispute_leaves_the_day_listed_again(self):
        attendance = self._make_attendance(geo_fence_violation=True)
        pending = RegularizationRequest.objects.create(
            employee=self.employee, attendance=attendance,
            reason_code=RegularizationRequest.REASON_GEO_VIOLATION_DISPUTE,
            reason="Dispute.",
        )
        pending.status = RegularizationRequest.STATUS_REJECTED
        pending.save()
        response = self.client.get(self.LIST_URL, **self.HX)
        self.assertContains(response, str(attendance.attendance_date))


class RaiseRequestPrefillTests(RegularizableAttendanceTestBase):
    def test_form_prefills_and_locks_attendance_and_narrows_reason(self):
        attendance = self._make_attendance(geo_fence_violation=True)
        response = self.client.get(f"{self.FORM_URL}?attendance={attendance.pk}", **self.HX)
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        # Only the geo-violation reason should remain selectable.
        self.assertIn("Geo-location Dispute", content)
        self.assertNotIn("Time Correction", content)
        self.assertNotIn("Overtime Decision Dispute", content)

    def test_submitting_via_the_prefilled_url_uses_the_locked_attendance(self):
        flagged = self._make_attendance(geo_fence_violation=True)
        other = self._make_attendance(day_offset=1)

        response = self.client.post(
            f"{self.FORM_URL}?attendance={flagged.pk}",
            data={
                # Tampered: a different, unflagged attendance id in the
                # POST body -- the disabled field must ignore this and
                # use the locked/initial value instead.
                "attendance": other.pk,
                "reason_code": RegularizationRequest.REASON_GEO_VIOLATION_DISPUTE,
                "reason": "Was outside the boundary but on a client site.",
            },
            **self.HX,
        )
        self.assertEqual(response.status_code, 200)
        request = RegularizationRequest.objects.get(employee=self.employee)
        self.assertEqual(request.attendance_id, flagged.pk)
        self.assertEqual(
            request.reason_code, RegularizationRequest.REASON_GEO_VIOLATION_DISPUTE
        )

    def test_both_reasons_open_when_both_flags_are_set(self):
        attendance = self._make_attendance(
            geo_fence_violation=True, overtime_second=3600,
            attendance_overtime_approve=False, attendance_validated=False,
        )
        response = self.client.get(f"{self.FORM_URL}?attendance={attendance.pk}", **self.HX)
        content = response.content.decode()
        self.assertIn("Geo-location Dispute", content)
        self.assertIn("Overtime Decision Dispute", content)

    def test_form_without_attendance_param_still_offers_the_generic_choices(self):
        response = self.client.get(self.FORM_URL, **self.HX)
        content = response.content.decode()
        self.assertIn("Time Correction", content)
        self.assertIn("Geo-location Dispute", content)
        self.assertIn("Overtime Decision Dispute", content)
