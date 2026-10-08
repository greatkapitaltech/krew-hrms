"""
Tests for the Auto Punch-out scheduler job (attendance/scheduler.py):
- the existing shift-based sweep, now gated by the new company-wide
  master switch (AttendanceGeneralSetting.auto_punch_out_enabled)
- the two cases it didn't cover before: Flexible-mode cutoff
  (AttendanceRuleSet.auto_punch_out_cutoff_time) and the no-shift flat
  cutoff (AttendanceGeneralSetting.no_shift_auto_punch_out_time)

_close_attendance is mocked throughout -- it drives the real clock_out()
view via the Request test helper (exercised elsewhere); these tests
isolate the SELECTION logic -- which open attendance rows get closed,
and under what setting -- from that.
"""

from datetime import date, time, timedelta
from unittest import mock

from django.test import TestCase

from attendance.models import Attendance, AttendanceGeneralSetting, AttendanceRuleSet
from attendance.scheduler import auto_punch_out
from base.models import COLLAR_WHITE, EmployeeShift, EmployeeType
from horilla.testkit.factories import make_company, make_employee, make_user


def _make_open_attendance(employee, **kwargs):
    defaults = dict(
        employee_id=employee,
        attendance_date=date.today(),
        attendance_clock_in_date=date.today(),
        attendance_clock_in=time(9, 0),
        attendance_clock_out=None,
        attendance_clock_out_date=None,
    )
    defaults.update(kwargs)
    # Mirrors clock_in_attendance_and_activity(): the scheduler now reads
    # exclusively from the frozen snapshot, never a live get_effective_
    # values() call, so a directly-constructed test row has to carry it
    # too if a rule_set was passed.
    rule_set = defaults.get("attendance_rule_set")
    if rule_set is not None and "attendance_rule_set_snapshot" not in defaults:
        defaults["attendance_rule_set_snapshot"] = AttendanceRuleSet.capture_snapshot(
            rule_set
        )
    return Attendance.objects.create(**defaults)


class AutoPunchOutMasterSwitchTests(TestCase):
    def setUp(self):
        self.company = make_company("Acme")
        self.employee = make_employee(
            company=self.company, email="switch@test.horilla", user=make_user("switch"),
        )
        AttendanceGeneralSetting.objects.filter(company_id=self.company).update(
            no_shift_auto_punch_out_time=time(0, 0)
        )

    @mock.patch("attendance.scheduler._close_attendance")
    def test_switch_off_blocks_the_no_shift_fallback(self, mocked_close):
        AttendanceGeneralSetting.objects.filter(company_id=self.company).update(
            auto_punch_out_enabled=False
        )
        _make_open_attendance(self.employee)
        auto_punch_out()
        mocked_close.assert_not_called()

    @mock.patch("attendance.scheduler._close_attendance")
    def test_switch_on_by_default_allows_the_no_shift_fallback(self, mocked_close):
        _make_open_attendance(self.employee)
        auto_punch_out()
        mocked_close.assert_called_once()

    @mock.patch("attendance.scheduler._close_attendance")
    def test_switch_off_blocks_the_flexible_mode_cutoff(self, mocked_close):
        AttendanceGeneralSetting.objects.filter(company_id=self.company).update(
            auto_punch_out_enabled=False
        )
        rule_set = AttendanceRuleSet.objects.create(
            tier="COMPANY", company=self.company,
            mode=AttendanceRuleSet.MODE_FLEXIBLE,
            auto_punch_out_cutoff_time=time(0, 0),
        )
        _make_open_attendance(self.employee, attendance_rule_set=rule_set)
        auto_punch_out()
        mocked_close.assert_not_called()


class AutoPunchOutFlexibleModeTests(TestCase):
    def setUp(self):
        self.company = make_company("Acme")
        self.employee = make_employee(
            company=self.company, email="flex@test.horilla", user=make_user("flex"),
        )

    @mock.patch("attendance.scheduler._close_attendance")
    def test_past_cutoff_gets_closed(self, mocked_close):
        rule_set = AttendanceRuleSet.objects.create(
            tier="COMPANY", company=self.company,
            mode=AttendanceRuleSet.MODE_FLEXIBLE,
            auto_punch_out_cutoff_time=time(0, 0),
        )
        attendance = _make_open_attendance(self.employee, attendance_rule_set=rule_set)
        auto_punch_out()
        mocked_close.assert_called_once()
        self.assertEqual(mocked_close.call_args[0][0], attendance)

    @mock.patch("attendance.scheduler._close_attendance")
    def test_before_cutoff_is_left_open(self, mocked_close):
        rule_set = AttendanceRuleSet.objects.create(
            tier="COMPANY", company=self.company,
            mode=AttendanceRuleSet.MODE_FLEXIBLE,
            auto_punch_out_cutoff_time=time(23, 59),
        )
        _make_open_attendance(
            self.employee, attendance_rule_set=rule_set,
            attendance_date=date.today() + timedelta(days=1),
        )
        auto_punch_out()
        mocked_close.assert_not_called()

    @mock.patch("attendance.scheduler._close_attendance")
    def test_no_cutoff_configured_is_left_open(self, mocked_close):
        rule_set = AttendanceRuleSet.objects.create(
            tier="COMPANY", company=self.company,
            mode=AttendanceRuleSet.MODE_FLEXIBLE,
        )
        _make_open_attendance(self.employee, attendance_rule_set=rule_set)
        auto_punch_out()
        mocked_close.assert_not_called()

    @mock.patch("attendance.scheduler._close_attendance")
    def test_employee_type_override_inherits_cutoff_from_company_default(
        self, mocked_close
    ):
        AttendanceRuleSet.objects.create(
            tier="COMPANY", company=self.company,
            mode=AttendanceRuleSet.MODE_SHIFT_BASED,
            auto_punch_out_cutoff_time=time(0, 0),
        )
        emp_type = EmployeeType.objects.create(
            employee_type="Contract", collar_category=COLLAR_WHITE
        )
        emp_type.company_id.add(self.company)
        override = AttendanceRuleSet.objects.create(
            tier="EMPLOYEE_TYPE", company=self.company,
            employee_type_category=COLLAR_WHITE,
            mode=AttendanceRuleSet.MODE_FLEXIBLE,
            # auto_punch_out_cutoff_time left blank -> inherits from
            # the Company Default row above.
        )
        _make_open_attendance(self.employee, attendance_rule_set=override)
        auto_punch_out()
        mocked_close.assert_called_once()


class AutoPunchOutNoShiftFallbackTests(TestCase):
    def setUp(self):
        self.company = make_company("Acme")
        self.employee = make_employee(
            company=self.company, email="noshift@test.horilla", user=make_user("noshift"),
        )

    @mock.patch("attendance.scheduler._close_attendance")
    def test_uses_the_company_settings_cutoff(self, mocked_close):
        AttendanceGeneralSetting.objects.filter(company_id=self.company).update(
            no_shift_auto_punch_out_time=time(0, 0)
        )
        attendance = _make_open_attendance(self.employee)
        auto_punch_out()
        mocked_close.assert_called_once()
        self.assertEqual(mocked_close.call_args[0][0], attendance)

    @mock.patch("attendance.scheduler._close_attendance")
    def test_before_cutoff_is_left_open(self, mocked_close):
        AttendanceGeneralSetting.objects.filter(company_id=self.company).update(
            no_shift_auto_punch_out_time=time(23, 59)
        )
        _make_open_attendance(
            self.employee, attendance_date=date.today() + timedelta(days=1),
        )
        auto_punch_out()
        mocked_close.assert_not_called()

    @mock.patch("attendance.scheduler._close_attendance")
    def test_shift_assigned_employee_is_not_touched_by_this_branch(self, mocked_close):
        # Shift-based (not Flexible) and has a real shift -- belongs to
        # the shift-based sweep. This loop must leave it alone even
        # though that shift's own schedule never enabled auto punch-out,
        # rather than silently applying the no-shift flat cutoff to it.
        shift = EmployeeShift.objects.create(employee_shift="Day Shift")
        shift.company_id.add(self.company)
        employee = make_employee(
            company=self.company, email="hasshift@test.horilla",
            user=make_user("hasshift"), shift=shift,
        )
        AttendanceGeneralSetting.objects.filter(company_id=self.company).update(
            no_shift_auto_punch_out_time=time(0, 0)
        )
        _make_open_attendance(employee, shift_id=shift)
        auto_punch_out()
        mocked_close.assert_not_called()
