"""
Tests for attendance_validate() (attendance/views/views.py) now reading
its threshold from the snapshotted AttendanceRuleSet row instead of the
old single-row, one-per-installation AttendanceValidationCondition table.
"""

from datetime import date, timedelta

from django.test import TestCase

from attendance.models import Attendance, AttendanceRuleSet
from attendance.views.views import attendance_validate
from base.models import COLLAR_WHITE, EmployeeType
from horilla.testkit.factories import make_company, make_employee, make_user


class AttendanceValidateThresholdTests(TestCase):
    def setUp(self):
        self.company = make_company("Acme")
        self.employee = make_employee(
            company=self.company, email="valid1@test.horilla", user=make_user("valid1"),
        )

    def _make_attendance(self, worked_hour, rule_set=None, day_offset=0):
        # One Attendance row per (employee, date) -- distinct offsets let
        # a single test create more than one without colliding. Mirrors
        # clock_in_attendance_and_activity(): attendance_validate() now
        # reads exclusively from the frozen snapshot.
        return Attendance.objects.create(
            employee_id=self.employee,
            attendance_date=date.today() - timedelta(days=day_offset),
            attendance_worked_hour=worked_hour,
            attendance_rule_set=rule_set,
            attendance_rule_set_snapshot=AttendanceRuleSet.capture_snapshot(rule_set),
        )

    def test_no_rule_set_falls_back_to_default_nine_hours(self):
        under = self._make_attendance("08:30", day_offset=0)
        over = self._make_attendance("09:30", day_offset=1)
        self.assertTrue(attendance_validate(under))
        self.assertFalse(attendance_validate(over))

    def test_company_default_threshold_is_used(self):
        rule_set = AttendanceRuleSet.objects.create(
            tier="COMPANY", company=self.company, validation_threshold="08:00",
        )
        under = self._make_attendance("07:45", rule_set=rule_set, day_offset=0)
        over = self._make_attendance("08:15", rule_set=rule_set, day_offset=1)
        self.assertTrue(attendance_validate(under))
        self.assertFalse(attendance_validate(over))

    def test_worked_hours_exactly_at_threshold_validates(self):
        rule_set = AttendanceRuleSet.objects.create(
            tier="COMPANY", company=self.company, validation_threshold="08:00",
        )
        exact = self._make_attendance("08:00", rule_set=rule_set)
        self.assertTrue(attendance_validate(exact))

    def test_employee_type_override_inherits_blank_threshold(self):
        AttendanceRuleSet.objects.create(
            tier="COMPANY", company=self.company, validation_threshold="08:00",
        )
        emp_type = EmployeeType.objects.create(
            employee_type="Contract", collar_category=COLLAR_WHITE
        )
        emp_type.company_id.add(self.company)
        override = AttendanceRuleSet.objects.create(
            tier="EMPLOYEE_TYPE", company=self.company,
            employee_type_category=COLLAR_WHITE,
            # validation_threshold left blank -> inherits 08:00
        )
        over_company_default_but_under_override = self._make_attendance(
            "08:15", rule_set=override
        )
        self.assertFalse(attendance_validate(over_company_default_but_under_override))

    def test_employee_type_override_with_its_own_threshold_is_not_overwritten(self):
        AttendanceRuleSet.objects.create(
            tier="COMPANY", company=self.company, validation_threshold="08:00",
        )
        emp_type = EmployeeType.objects.create(
            employee_type="Contract", collar_category=COLLAR_WHITE
        )
        emp_type.company_id.add(self.company)
        override = AttendanceRuleSet.objects.create(
            tier="EMPLOYEE_TYPE", company=self.company,
            employee_type_category=COLLAR_WHITE, validation_threshold="10:00",
        )
        attendance = self._make_attendance("09:00", rule_set=override)
        self.assertTrue(attendance_validate(attendance))

    def test_rule_set_with_no_threshold_and_no_company_default_falls_back_to_default(
        self,
    ):
        rule_set = AttendanceRuleSet.objects.create(
            tier="COMPANY", company=self.company,
        )  # validation_threshold left blank, no other row to inherit from
        under = self._make_attendance("08:30", rule_set=rule_set, day_offset=0)
        over = self._make_attendance("09:30", rule_set=rule_set, day_offset=1)
        self.assertTrue(attendance_validate(under))
        self.assertFalse(attendance_validate(over))
