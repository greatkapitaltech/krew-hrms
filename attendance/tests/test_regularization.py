"""
Tests for Attendance Regularization (#8):
- RegularizationRequest: durable dispute record, monthly cap (rejected
  requests count too, unlike the old flag-based workflow it doesn't
  replace), and type-specific approve()/reject() side effects.
- ApprovalDelegate: shared approval-authority hand-off, gated by a
  dedicated permission, either for a date range or one specific request.
"""

from datetime import date, time, timedelta

from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.test import TestCase

from attendance.models import (
    ApprovalDelegate,
    Attendance,
    AttendanceRuleSet,
    RegularizationRequest,
)
from employee.models import EmployeeWorkInformation
from horilla.testkit.factories import make_company, make_employee, make_user


def _set_reporting_manager(employee, manager):
    EmployeeWorkInformation.objects.filter(employee_id=employee).update(
        reporting_manager_id=manager
    )


def _grant_delegate_permission(user):
    perm = Permission.objects.get(
        codename="can_be_delegate",
        content_type=ContentType.objects.get_for_model(ApprovalDelegate),
    )
    user.user_permissions.add(perm)
    return type(user).objects.get(pk=user.pk)  # fresh instance, clears perm cache


class RegularizationCanRaiseTests(TestCase):
    def setUp(self):
        self.company = make_company("Acme")
        self.employee = make_employee(
            company=self.company, email="reg1@test.horilla", user=make_user("reg1"),
        )
        self.attendance = Attendance.objects.create(
            employee_id=self.employee, attendance_date=date(2026, 9, 21),
        )

    def _make_request(self, day_offset=0, status=RegularizationRequest.STATUS_PENDING):
        return RegularizationRequest.objects.create(
            employee=self.employee,
            attendance=self.attendance,
            reason_code=RegularizationRequest.REASON_TIME_CORRECTION,
            reason="Forgot to clock in on time",
            corrected_clock_in=time(9, 0),
            status=status,
        )

    def test_disabled_by_default(self):
        # No AttendanceRuleSet at all -- opt-in, not open by default.
        allowed, reason = RegularizationRequest.can_raise(self.employee)
        self.assertFalse(allowed)
        self.assertIsNotNone(reason)

    def test_enabled_with_no_cap_always_allows(self):
        AttendanceRuleSet.objects.create(
            tier="COMPANY", company=self.company, regularization_enabled=True,
        )
        for _ in range(5):
            self._make_request()
        allowed, reason = RegularizationRequest.can_raise(self.employee)
        self.assertTrue(allowed)

    def test_cap_counts_rejected_requests_too(self):
        AttendanceRuleSet.objects.create(
            tier="COMPANY", company=self.company, regularization_enabled=True,
            regularization_monthly_cap=2,
        )
        self._make_request(status=RegularizationRequest.STATUS_REJECTED)
        self._make_request(status=RegularizationRequest.STATUS_PENDING)
        allowed, reason = RegularizationRequest.can_raise(self.employee)
        self.assertFalse(allowed)
        self.assertIsNotNone(reason)

    def test_under_cap_is_allowed(self):
        AttendanceRuleSet.objects.create(
            tier="COMPANY", company=self.company, regularization_enabled=True,
            regularization_monthly_cap=2,
        )
        self._make_request()
        allowed, reason = RegularizationRequest.can_raise(self.employee)
        self.assertTrue(allowed)


class RegularizationApproveRejectTests(TestCase):
    def setUp(self):
        self.company = make_company("Acme")
        self.employee = make_employee(
            company=self.company, email="reg2@test.horilla", user=make_user("reg2"),
        )
        self.manager = make_employee(
            company=self.company, email="mgr1@test.horilla", user=make_user("mgr1"),
        )
        self.attendance = Attendance.objects.create(
            employee_id=self.employee,
            attendance_date=date(2026, 9, 21),
            attendance_worked_hour="07:00",
        )

    def test_time_correction_applies_and_recomputes_worked_hour(self):
        request = RegularizationRequest.objects.create(
            employee=self.employee, attendance=self.attendance,
            reason_code=RegularizationRequest.REASON_TIME_CORRECTION,
            reason="Forgot to clock in on time",
            corrected_clock_in=time(9, 0),
            corrected_clock_in_date=date(2026, 9, 21),
            corrected_clock_out=time(18, 0),
            corrected_clock_out_date=date(2026, 9, 21),
        )
        request.approve(self.manager)

        self.attendance.refresh_from_db()
        self.assertEqual(self.attendance.attendance_clock_in, time(9, 0))
        self.assertEqual(self.attendance.attendance_clock_out, time(18, 0))
        self.assertEqual(self.attendance.attendance_worked_hour, "09:00")

        request.refresh_from_db()
        self.assertEqual(request.status, RegularizationRequest.STATUS_APPROVED)
        self.assertEqual(request.resolved_by, self.manager)
        self.assertIsNotNone(request.resolved_at)

    def test_geo_violation_dispute_clears_the_geo_flags(self):
        self.attendance.geo_fence_violation = True
        self.attendance.geo_fence_unverified = True
        self.attendance.save()

        request = RegularizationRequest.objects.create(
            employee=self.employee, attendance=self.attendance,
            reason_code=RegularizationRequest.REASON_GEO_VIOLATION_DISPUTE,
            reason="I was inside the office, GPS was just off",
        )
        request.approve(self.manager)

        self.attendance.refresh_from_db()
        self.assertFalse(self.attendance.geo_fence_violation)
        self.assertFalse(self.attendance.geo_fence_unverified)

    def test_overtime_denial_dispute_approves_the_overtime(self):
        self.attendance.attendance_overtime_approve = False
        self.attendance.overtime_second = 3600
        self.attendance.save()

        request = RegularizationRequest.objects.create(
            employee=self.employee, attendance=self.attendance,
            reason_code=RegularizationRequest.REASON_OVERTIME_DENIAL_DISPUTE,
            reason="This overtime was pre-approved by my manager verbally",
        )
        request.approve(self.manager)

        self.attendance.refresh_from_db()
        self.assertTrue(self.attendance.attendance_overtime_approve)
        request.refresh_from_db()
        self.assertEqual(
            request.overtime_decision, RegularizationRequest.OT_DECISION_APPROVED
        )

    def test_auto_close_dispute_is_approved_with_no_attendance_side_effect(self):
        request = RegularizationRequest.objects.create(
            employee=self.employee, attendance=self.attendance,
            reason_code=RegularizationRequest.REASON_AUTO_CLOSE_DISPUTE,
            reason="I was still working, forgot to clock out",
        )
        request.approve(self.manager)
        request.refresh_from_db()
        self.assertEqual(request.status, RegularizationRequest.STATUS_APPROVED)

    def test_approve_is_a_no_op_once_already_resolved(self):
        request = RegularizationRequest.objects.create(
            employee=self.employee, attendance=self.attendance,
            reason_code=RegularizationRequest.REASON_AUTO_CLOSE_DISPUTE,
            reason="dispute",
        )
        request.reject(self.manager, resolution_note="Not valid")
        request.approve(self.manager)  # must not flip a rejected request
        request.refresh_from_db()
        self.assertEqual(request.status, RegularizationRequest.STATUS_REJECTED)

    def test_reject_forces_overtime_decision_to_denied(self):
        request = RegularizationRequest.objects.create(
            employee=self.employee, attendance=self.attendance,
            reason_code=RegularizationRequest.REASON_OVERTIME_DENIAL_DISPUTE,
            reason="dispute",
        )
        request.reject(self.manager, resolution_note="Overtime was not authorized")
        request.refresh_from_db()
        self.assertEqual(request.status, RegularizationRequest.STATUS_REJECTED)
        self.assertEqual(
            request.overtime_decision, RegularizationRequest.OT_DECISION_DENIED
        )

    def test_time_correction_requires_at_least_one_corrected_time(self):
        request = RegularizationRequest(
            employee=self.employee, attendance=self.attendance,
            reason_code=RegularizationRequest.REASON_TIME_CORRECTION,
            reason="dispute",
        )
        with self.assertRaises(ValidationError):
            request.full_clean()


class ApprovalDelegateCleanTests(TestCase):
    def setUp(self):
        self.company = make_company("Acme")
        self.manager = make_employee(
            company=self.company, email="mgr2@test.horilla", user=make_user("mgr2"),
        )
        self.delegate_employee = make_employee(
            company=self.company, email="del1@test.horilla", user=make_user("del1"),
        )
        _grant_delegate_permission(self.delegate_employee.employee_user_id)

    def test_valid_date_range_delegation_passes(self):
        delegation = ApprovalDelegate(
            delegator=self.manager, delegate=self.delegate_employee,
            start_date=date(2026, 9, 1), end_date=date(2026, 9, 30),
        )
        delegation.full_clean()  # must not raise

    def test_self_delegation_rejected(self):
        delegation = ApprovalDelegate(
            delegator=self.manager, delegate=self.manager,
            start_date=date(2026, 9, 1), end_date=date(2026, 9, 30),
        )
        with self.assertRaises(ValidationError):
            delegation.full_clean()

    def test_neither_target_nor_range_rejected(self):
        delegation = ApprovalDelegate(
            delegator=self.manager, delegate=self.delegate_employee,
        )
        with self.assertRaises(ValidationError):
            delegation.full_clean()

    def test_both_target_and_range_rejected(self):
        request = RegularizationRequest.objects.create(
            employee=self.delegate_employee,
            attendance=Attendance.objects.create(
                employee_id=self.delegate_employee, attendance_date=date(2026, 9, 21),
            ),
            reason_code=RegularizationRequest.REASON_AUTO_CLOSE_DISPUTE,
            reason="dispute",
        )
        delegation = ApprovalDelegate(
            delegator=self.manager, delegate=self.delegate_employee,
            start_date=date(2026, 9, 1), end_date=date(2026, 9, 30),
            content_type=ContentType.objects.get_for_model(RegularizationRequest),
            object_id=request.pk,
        )
        with self.assertRaises(ValidationError):
            delegation.full_clean()

    def test_delegate_without_permission_rejected(self):
        unprivileged = make_employee(
            company=self.company, email="noperm1@test.horilla",
            user=make_user("noperm1"),
        )
        delegation = ApprovalDelegate(
            delegator=self.manager, delegate=unprivileged,
            start_date=date(2026, 9, 1), end_date=date(2026, 9, 30),
        )
        with self.assertRaises(ValidationError):
            delegation.full_clean()


class ApprovalDelegateAuthorizationTests(TestCase):
    def setUp(self):
        self.company = make_company("Acme")
        self.manager = make_employee(
            company=self.company, email="mgr3@test.horilla", user=make_user("mgr3"),
        )
        self.employee = make_employee(
            company=self.company, email="emp3@test.horilla", user=make_user("emp3"),
        )
        _set_reporting_manager(self.employee, self.manager)
        # .filter().update() above doesn't refresh self.employee's
        # already-cached employee_work_info reverse relation.
        from employee.models import Employee

        self.employee = Employee.objects.get(pk=self.employee.pk)
        self.delegate_employee = make_employee(
            company=self.company, email="del2@test.horilla", user=make_user("del2"),
        )
        _grant_delegate_permission(self.delegate_employee.employee_user_id)

    def test_direct_reporting_manager_can_approve(self):
        self.assertTrue(
            ApprovalDelegate.can_approve(self.manager, self.employee)
        )

    def test_unrelated_employee_cannot_approve(self):
        stranger = make_employee(
            company=self.company, email="stranger1@test.horilla",
            user=make_user("stranger1"),
        )
        self.assertFalse(
            ApprovalDelegate.can_approve(stranger, self.employee)
        )

    def test_employee_cannot_approve_their_own_request(self):
        self.assertFalse(
            ApprovalDelegate.can_approve(self.employee, self.employee)
        )

    def test_active_date_range_delegate_can_approve(self):
        today = date(2026, 9, 21)
        ApprovalDelegate.objects.create(
            delegator=self.manager, delegate=self.delegate_employee,
            start_date=today - timedelta(days=1), end_date=today + timedelta(days=1),
        )
        self.assertTrue(
            ApprovalDelegate.can_approve(
                self.delegate_employee, self.employee, on_date=today,
            )
        )

    def test_delegate_outside_date_range_cannot_approve(self):
        today = date(2026, 9, 21)
        ApprovalDelegate.objects.create(
            delegator=self.manager, delegate=self.delegate_employee,
            start_date=today + timedelta(days=5), end_date=today + timedelta(days=10),
        )
        self.assertFalse(
            ApprovalDelegate.can_approve(
                self.delegate_employee, self.employee, on_date=today,
            )
        )

    def test_delegate_for_a_specific_request_only_covers_that_request(self):
        attendance = Attendance.objects.create(
            employee_id=self.employee, attendance_date=date(2026, 9, 21),
        )
        request = RegularizationRequest.objects.create(
            employee=self.employee, attendance=attendance,
            reason_code=RegularizationRequest.REASON_AUTO_CLOSE_DISPUTE,
            reason="dispute",
        )
        other_request = RegularizationRequest.objects.create(
            employee=self.employee, attendance=attendance,
            reason_code=RegularizationRequest.REASON_AUTO_CLOSE_DISPUTE,
            reason="a different dispute",
        )
        ApprovalDelegate.objects.create(
            delegator=self.manager, delegate=self.delegate_employee,
            content_type=ContentType.objects.get_for_model(RegularizationRequest),
            object_id=request.pk,
        )
        self.assertTrue(
            ApprovalDelegate.can_approve(
                self.delegate_employee, self.employee, target=request,
            )
        )
        self.assertFalse(
            ApprovalDelegate.can_approve(
                self.delegate_employee, self.employee, target=other_request,
            )
        )

    def test_inactive_delegation_does_not_authorize(self):
        today = date(2026, 9, 21)
        ApprovalDelegate.objects.create(
            delegator=self.manager, delegate=self.delegate_employee,
            start_date=today - timedelta(days=1), end_date=today + timedelta(days=1),
            is_active=False,
        )
        self.assertFalse(
            ApprovalDelegate.can_approve(
                self.delegate_employee, self.employee, on_date=today,
            )
        )
