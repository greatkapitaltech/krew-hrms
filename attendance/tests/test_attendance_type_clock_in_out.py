"""
Tests for wiring Attendance Type resolution into clock-in/clock-out:
- the resolved AttendanceRuleSet is snapshotted once, at the first
  clock-in of the day, and never re-resolved afterward
- late-come/early-out detection is skipped entirely under Flexible mode
"""

from datetime import date, datetime, time
from unittest import mock

from django.test import TestCase

from attendance.models import Attendance, AttendanceRuleSet
from attendance.views.clock_in_out import clock_in_attendance_and_activity
from base.models import Department, EmployeeShift, EmployeeShiftDay, EmployeeShiftSchedule
from employee.models import EmployeeWorkInformation
from horilla.testkit.factories import make_company, make_employee, make_user


class AttendanceModeHelperTests(TestCase):
    def setUp(self):
        self.company = make_company("Acme")
        self.employee = make_employee(
            company=self.company, email="emp@test.horilla", user=make_user("emp"),
        )

    def test_defaults_to_shift_based_when_nothing_snapshotted(self):
        attendance = Attendance(
            employee_id=self.employee, attendance_date=date.today(),
        )
        self.assertEqual(
            attendance.get_attendance_mode(), AttendanceRuleSet.MODE_SHIFT_BASED
        )
        self.assertFalse(attendance.is_flexible_mode())

    def test_reflects_snapshotted_flexible_rule_set(self):
        rule_set = AttendanceRuleSet.objects.create(
            tier="COMPANY", company=self.company, mode=AttendanceRuleSet.MODE_FLEXIBLE,
        )
        attendance = Attendance(
            employee_id=self.employee, attendance_date=date.today(),
            attendance_rule_set=rule_set,
        )
        self.assertTrue(attendance.is_flexible_mode())


class ClockInSnapshotsRuleSetTests(TestCase):
    def setUp(self):
        self.company = make_company("Acme")
        self.shift = EmployeeShift.objects.create(employee_shift="Day Shift")
        self.shift.company_id.add(self.company)
        self.day = EmployeeShiftDay.objects.create(day="monday")
        EmployeeShiftSchedule.objects.create(
            day=self.day, shift_id=self.shift,
            minimum_working_hour="08:00",
            start_time=time(9, 0), end_time=time(17, 0),
        )
        self.employee = make_employee(
            company=self.company, email="emp@test.horilla", user=make_user("emp"),
            shift=self.shift,
        )

    def _clock_in(self):
        return clock_in_attendance_and_activity(
            employee=self.employee,
            date_today=date(2026, 9, 21),  # a Monday
            attendance_date=date(2026, 9, 21),
            day=self.day,
            now="09:05",
            shift=self.shift,
            minimum_hour="08:00",
            start_time=32400,  # 09:00 in seconds
            end_time=61200,  # 17:00 in seconds
            in_datetime=timezone_aware(datetime(2026, 9, 21, 9, 5)),
        )

    def test_new_attendance_snapshots_the_resolved_rule_set(self):
        rule_set = AttendanceRuleSet.objects.create(
            tier="COMPANY", company=self.company,
            mode=AttendanceRuleSet.MODE_SHIFT_BASED,
        )
        attendance = self._clock_in()
        self.assertEqual(attendance.attendance_rule_set_id, rule_set.pk)

    def test_reclocking_in_same_day_does_not_overwrite_the_snapshot(self):
        company_default = AttendanceRuleSet.objects.create(
            tier="COMPANY", company=self.company,
            mode=AttendanceRuleSet.MODE_SHIFT_BASED,
        )
        attendance = self._clock_in()
        self.assertEqual(attendance.attendance_rule_set_id, company_default.pk)

        # A new, more specific override appears between the two clock-ins
        # on the same day -- if resolution re-ran, it would now win.
        dept = Department.objects.create(department="Warehouse")
        dept.company_id.add(self.company)
        EmployeeWorkInformation.objects.filter(employee_id=self.employee).update(
            department_id=dept
        )
        dept_override = AttendanceRuleSet.objects.create(
            tier="DEPARTMENT", company=self.company, department=dept,
            mode=AttendanceRuleSet.MODE_FLEXIBLE,
        )

        attendance_again = self._clock_in()
        self.assertEqual(attendance_again.attendance_rule_set_id, company_default.pk)
        self.assertNotEqual(
            attendance_again.attendance_rule_set_id, dept_override.pk
        )

    def test_no_rule_set_configured_leaves_snapshot_null(self):
        attendance = self._clock_in()
        self.assertIsNone(attendance.attendance_rule_set_id)
        self.assertEqual(
            attendance.get_attendance_mode(), AttendanceRuleSet.MODE_SHIFT_BASED
        )


class LateComeGatingTests(TestCase):
    """
    Targeted test of the mode gate itself: does clock_in_attendance_and_
    activity() call late_come() or not, based on the snapshotted mode.
    Calls the plain function directly (proven reliable above), with
    late_come() mocked out -- this isolates the gating conditional I
    added from the pre-existing lateness-computation logic inside
    late_come() itself, which is unrelated to this change.
    """

    def setUp(self):
        self.company = make_company("Acme")
        self.shift = EmployeeShift.objects.create(employee_shift="Day Shift")
        self.shift.company_id.add(self.company)
        self.day = EmployeeShiftDay.objects.create(day="monday")
        EmployeeShiftSchedule.objects.create(
            day=self.day, shift_id=self.shift,
            minimum_working_hour="08:00",
            start_time=time(9, 0), end_time=time(17, 0),
        )
        self.employee = make_employee(
            company=self.company, email="emp@test.horilla", user=make_user("emp"),
            shift=self.shift,
        )

    def _clock_in(self):
        return clock_in_attendance_and_activity(
            employee=self.employee,
            date_today=date(2026, 9, 21),
            attendance_date=date(2026, 9, 21),
            day=self.day,
            now="10:30",
            shift=self.shift,
            minimum_hour="08:00",
            start_time=32400,
            end_time=61200,
            in_datetime=timezone_aware(datetime(2026, 9, 21, 10, 30)),
        )

    def test_shift_based_mode_calls_late_come(self):
        AttendanceRuleSet.objects.create(
            tier="COMPANY", company=self.company,
            mode=AttendanceRuleSet.MODE_SHIFT_BASED,
        )
        with mock.patch(
            "attendance.views.clock_in_out.late_come"
        ) as mocked_late_come:
            self._clock_in()
        mocked_late_come.assert_called_once()

    def test_flexible_mode_skips_late_come_entirely(self):
        AttendanceRuleSet.objects.create(
            tier="COMPANY", company=self.company,
            mode=AttendanceRuleSet.MODE_FLEXIBLE,
        )
        with mock.patch(
            "attendance.views.clock_in_out.late_come"
        ) as mocked_late_come:
            self._clock_in()
        mocked_late_come.assert_not_called()

    def test_no_rule_set_defaults_to_shift_based_and_calls_late_come(self):
        # No AttendanceRuleSet configured at all -- get_attendance_mode()
        # falls back to Shift-based, so late_come() must still run.
        with mock.patch(
            "attendance.views.clock_in_out.late_come"
        ) as mocked_late_come:
            self._clock_in()
        mocked_late_come.assert_called_once()


def timezone_aware(naive_dt):
    from django.utils import timezone

    return timezone.make_aware(naive_dt)
