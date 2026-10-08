"""
Web-view tests for Payroll Readiness (attendance/cbv/payroll_readiness.py)
-- Live Preview (always a live query over WorkRecords/Attendance) and
Final Report ("Lock Forever", freezing a PayrollReadinessSnapshot that
can never be re-created for the same period or edited afterward).

WorkRecords itself is pre-existing, upstream code -- kept live by
attendance_post_save() in attendance/signals.py, which fires on every
Attendance.save(). These tests create real Attendance rows and let
that signal populate WorkRecords naturally, the same way production
data actually gets there, rather than hand-crafting WorkRecords rows
directly (which the signal would just overwrite on the next save
anyway).
"""

from datetime import date, timedelta

from django.test import TestCase

from attendance.cbv.payroll_readiness import PayrollReadinessSnapshotRow, _is_exception
from attendance.models import (
    Attendance,
    AttendanceRuleSet,
    PayrollReadinessSnapshot,
    RegularizationRequest,
    WorkRecords,
)
from horilla.horilla_middlewares import _thread_locals
from horilla.testkit.company import clear_selected_company
from horilla.testkit.factories import make_company, make_employee, make_user


class PayrollReadinessTestBase(TestCase):
    PAGE_URL = "/attendance/payroll-readiness/"
    LIVE_PREVIEW_URL = "/attendance/payroll-readiness/live-preview/"
    FINAL_REPORT_URL = "/attendance/payroll-readiness/final-report/"
    LOCK_URL = "/attendance/payroll-readiness/lock/"
    HX = {"HTTP_HX_REQUEST": "true"}

    def setUp(self):
        _thread_locals.request = None
        clear_selected_company()
        self.addCleanup(self._clear_context_vars)

        self.company = make_company("Payroll Readiness Co")
        self.admin_user = make_user("payroll_admin", is_superuser=True)
        self.admin = make_employee(
            company=self.company, email="payroll_admin@test.horilla", user=self.admin_user,
        )
        self.employee = make_employee(
            company=self.company, email="payroll_employee@test.horilla", user=make_user("payroll_employee"),
        )
        self.client.force_login(self.admin_user)
        session = self.client.session
        session["selected_company"] = str(self.company.pk)
        session.save()

        AttendanceRuleSet.objects.create(
            tier="COMPANY", company=self.company, mode=AttendanceRuleSet.MODE_FLEXIBLE,
        )

    def _clear_context_vars(self):
        _thread_locals.request = None
        clear_selected_company()

    def _make_validated_attendance(self, employee, day, **overrides):
        defaults = {
            "employee_id": employee, "attendance_date": day,
            "attendance_clock_in": "09:00", "attendance_clock_in_date": day,
            "attendance_clock_out": "18:00", "attendance_clock_out_date": day,
            "attendance_worked_hour": "09:00",
            "attendance_validated": True,
        }
        defaults.update(overrides)
        return Attendance.objects.create(**defaults)


class PageAccessTests(PayrollReadinessTestBase):
    def test_page_and_tabs_render(self):
        self.assertEqual(self.client.get(self.PAGE_URL).status_code, 200)
        self.assertEqual(self.client.get(self.LIVE_PREVIEW_URL, **self.HX).status_code, 200)
        self.assertEqual(self.client.get(self.FINAL_REPORT_URL, **self.HX).status_code, 200)


class DayTypeTests(PayrollReadinessTestBase):
    def test_a_validated_present_day_is_not_an_exception(self):
        day = date.today() - timedelta(days=1)
        self._make_validated_attendance(self.employee, day)
        work_record = WorkRecords.objects.get(employee_id=self.employee, date=day)
        self.assertFalse(_is_exception(work_record))
        self.assertEqual(work_record.work_record_type, "FDP")

    def test_an_unvalidated_day_is_an_exception(self):
        day = date.today() - timedelta(days=1)
        self._make_validated_attendance(self.employee, day, attendance_validated=False)
        work_record = WorkRecords.objects.get(employee_id=self.employee, date=day)
        self.assertTrue(_is_exception(work_record))
        self.assertEqual(work_record.work_record_type, "CONF")

    def test_pending_overtime_is_an_exception_even_though_validated(self):
        day = date.today() - timedelta(days=1)
        attendance = self._make_validated_attendance(self.employee, day)
        # .update() bypasses Attendance.save()'s own
        # update_attendance_overtime() recomputation (which would
        # otherwise reset overtime_second back to 0 with no rule_set
        # configured) -- these two fields are plain data here, nothing
        # about this test needs the real calculation to run.
        Attendance.objects.filter(pk=attendance.pk).update(
            overtime_second=3600, overtime_decision=Attendance.OVERTIME_DECISION_PENDING,
        )
        work_record = WorkRecords.objects.get(employee_id=self.employee, date=day)
        # validated -> not CONF, but still an exception because overtime
        # is sitting PENDING -- exactly the gap WorkRecords' own CONF
        # flag can't see on its own.
        self.assertNotEqual(work_record.work_record_type, "CONF")
        self.assertTrue(_is_exception(work_record))

    def test_pending_regularization_is_an_exception(self):
        day = date.today() - timedelta(days=1)
        attendance = self._make_validated_attendance(self.employee, day)
        RegularizationRequest.objects.create(
            employee=self.employee, attendance=attendance,
            reason_code=RegularizationRequest.REASON_TIME_CORRECTION,
            reason="Forgot to punch out at the right time",
        )
        work_record = WorkRecords.objects.get(employee_id=self.employee, date=day)
        self.assertTrue(_is_exception(work_record))


class LivePreviewTests(PayrollReadinessTestBase):
    def test_present_day_shows_up_in_live_preview(self):
        day = date.today() - timedelta(days=1)
        self._make_validated_attendance(self.employee, day)
        response = self.client.get(
            self.LIVE_PREVIEW_URL,
            {"start_date": day.isoformat(), "end_date": day.isoformat()},
            **self.HX,
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, str(self.employee))


class LockForeverTests(PayrollReadinessTestBase):
    def test_locking_creates_a_snapshot_with_rows(self):
        day = date.today() - timedelta(days=1)
        self._make_validated_attendance(self.employee, day)
        response = self.client.post(
            self.LOCK_URL, {"start_date": day.isoformat(), "end_date": day.isoformat()},
        )
        self.assertEqual(response.status_code, 200)
        snapshot = PayrollReadinessSnapshot.objects.get(
            company=self.company, start_date=day, end_date=day,
        )
        row = snapshot.rows.get(employee=self.employee, date=day)
        self.assertEqual(row.day_type, PayrollReadinessSnapshotRow.DAY_WORKED)
        self.assertFalse(row.was_exception)

    def test_an_open_exception_is_zeroed_out_but_still_locks(self):
        day = date.today() - timedelta(days=1)
        self._make_validated_attendance(self.employee, day, attendance_validated=False)
        response = self.client.post(
            self.LOCK_URL, {"start_date": day.isoformat(), "end_date": day.isoformat()},
        )
        self.assertEqual(response.status_code, 200)
        snapshot = PayrollReadinessSnapshot.objects.get(
            company=self.company, start_date=day, end_date=day,
        )
        row = snapshot.rows.get(employee=self.employee, date=day)
        self.assertEqual(row.day_type, PayrollReadinessSnapshotRow.DAY_EXCEPTION)
        self.assertTrue(row.was_exception)
        self.assertEqual(row.worked_hours, "00:00")

    def test_locking_the_same_period_twice_is_rejected(self):
        day = date.today() - timedelta(days=1)
        self._make_validated_attendance(self.employee, day)
        self.client.post(
            self.LOCK_URL, {"start_date": day.isoformat(), "end_date": day.isoformat()},
        )
        self.assertEqual(
            PayrollReadinessSnapshot.objects.filter(
                company=self.company, start_date=day, end_date=day,
            ).count(),
            1,
        )
        self.client.post(
            self.LOCK_URL, {"start_date": day.isoformat(), "end_date": day.isoformat()},
        )
        self.assertEqual(
            PayrollReadinessSnapshot.objects.filter(
                company=self.company, start_date=day, end_date=day,
            ).count(),
            1,
        )

    def test_a_merely_overlapping_period_is_also_rejected(self):
        # Not an exact duplicate -- Sept 1-Oct 31 vs. Sept 1-Sept 30 --
        # a real gap found after shipping: the original check only
        # compared (start_date, end_date) for an exact match, so a
        # smaller period fully contained in an already-locked one
        # sailed straight through and would have double-counted days
        # across two snapshots.
        wide_start = date(2026, 9, 1)
        wide_end = date(2026, 10, 31)
        self.client.post(
            self.LOCK_URL, {"start_date": wide_start.isoformat(), "end_date": wide_end.isoformat()},
        )
        self.assertEqual(
            PayrollReadinessSnapshot.objects.filter(company=self.company).count(), 1,
        )

        narrow_start = date(2026, 9, 1)
        narrow_end = date(2026, 9, 30)
        self.client.post(
            self.LOCK_URL,
            {"start_date": narrow_start.isoformat(), "end_date": narrow_end.isoformat()},
        )
        self.assertEqual(
            PayrollReadinessSnapshot.objects.filter(company=self.company).count(), 1,
        )

    def test_an_adjacent_non_overlapping_period_is_allowed(self):
        first_start, first_end = date(2026, 9, 1), date(2026, 9, 30)
        second_start, second_end = date(2026, 10, 1), date(2026, 10, 31)
        self.client.post(
            self.LOCK_URL, {"start_date": first_start.isoformat(), "end_date": first_end.isoformat()},
        )
        self.client.post(
            self.LOCK_URL,
            {"start_date": second_start.isoformat(), "end_date": second_end.isoformat()},
        )
        self.assertEqual(
            PayrollReadinessSnapshot.objects.filter(company=self.company).count(), 2,
        )

    def test_snapshot_detail_renders_frozen_data(self):
        day = date.today() - timedelta(days=1)
        self._make_validated_attendance(self.employee, day)
        self.client.post(
            self.LOCK_URL, {"start_date": day.isoformat(), "end_date": day.isoformat()},
        )
        snapshot = PayrollReadinessSnapshot.objects.get(
            company=self.company, start_date=day, end_date=day,
        )
        response = self.client.get(
            f"/attendance/payroll-readiness/final-report/{snapshot.pk}/", **self.HX,
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, str(self.employee))
