"""
Unit tests for attendance_validate() (attendance/views/views.py) in
isolation -- it now derives auto-validate entirely from
overtime_second/attendance_overtime_approve, both computed by
Attendance.update_attendance_overtime()/handle_overtime_conditions().
The integration with that real pipeline (via an actual clock-in/clock-out,
with the overtime fields genuinely computed rather than set directly) is
covered separately by ValidationAndOvertimeTogetherTests in
test_overtime_calculation.py; this file only pins down attendance_validate()'s
own two-line decision.

Supersedes test_validation_threshold.py, which tested the old flat
"worked hours vs AttendanceRuleSet.validation_threshold" ceiling check --
that field (and the column backing it) has since been removed entirely;
see attendance_validate()'s own docstring for why it was dropped.
"""

from datetime import date

from django.test import TestCase

from attendance.models import Attendance
from attendance.views.views import attendance_validate
from horilla.testkit.factories import make_company, make_employee, make_user


class AttendanceValidateTests(TestCase):
    def setUp(self):
        self.company = make_company("Acme")
        self.employee = make_employee(
            company=self.company, email="validate1@test.horilla", user=make_user("validate1"),
        )

    def _make_attendance(self, overtime_second, overtime_approve):
        attendance = Attendance.objects.create(
            employee_id=self.employee,
            attendance_date=date.today(),
            attendance_worked_hour="08:00",
        )
        attendance.overtime_second = overtime_second
        attendance.attendance_overtime_approve = overtime_approve
        return attendance

    def test_no_overtime_validates_regardless_of_approve_flag(self):
        no_overtime = self._make_attendance(overtime_second=0, overtime_approve=False)
        self.assertTrue(attendance_validate(no_overtime))

    def test_none_overtime_second_validates(self):
        # overtime_second is nullable -- a row that never went through
        # update_attendance_overtime() at all (e.g. predates this
        # feature) shouldn't need review just for that.
        never_computed = self._make_attendance(overtime_second=None, overtime_approve=False)
        self.assertTrue(attendance_validate(never_computed))

    def test_overtime_within_buffer_validates(self):
        auto_approved = self._make_attendance(overtime_second=600, overtime_approve=True)
        self.assertTrue(attendance_validate(auto_approved))

    def test_overtime_beyond_buffer_needs_review(self):
        needs_review = self._make_attendance(overtime_second=2700, overtime_approve=False)
        self.assertFalse(attendance_validate(needs_review))
