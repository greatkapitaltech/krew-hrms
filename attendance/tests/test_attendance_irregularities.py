"""
Web-view tests for Attendance Irregularities' Screen 2
(attendance/cbv/attendance_irregularities.py) -- Instances and By
Employee, read-only, built on top of the existing
AttendanceLateComeEarlyOut record (the same one the older, penalty-
oriented "Late Arrival & Early Departure" screen uses), but without
that screen's shift-only exclusion or penalty actions.

Covers: a Shift-based late-come row's Expected/Actual/Delta, a
Flexible-mode hours-shortfall row (explicitly excluded from the older
screen, included here), the By Employee aggregate counts, and
manager-scoping (a manager sees their own reports' irregularities, not
everyone's, unless they hold the view permission).
"""

from datetime import date, timedelta

from django.test import TestCase

from attendance.models import Attendance, AttendanceLateComeEarlyOut, AttendanceRuleSet
from base.models import EmployeeShift, EmployeeShiftDay, EmployeeShiftSchedule
from employee.models import EmployeeWorkInformation
from horilla.horilla_middlewares import _thread_locals
from horilla.testkit.company import clear_selected_company
from horilla.testkit.factories import make_company, make_employee, make_user


class AttendanceIrregularitiesTestBase(TestCase):
    PAGE_URL = "/attendance/irregularities/"
    INSTANCES_URL = "/attendance/irregularities/instances/"
    NAV_URL = "/attendance/irregularities/nav/"
    BY_EMPLOYEE_URL = "/attendance/irregularities/by-employee/"
    HX = {"HTTP_HX_REQUEST": "true"}

    def setUp(self):
        _thread_locals.request = None
        clear_selected_company()
        self.addCleanup(self._clear_context_vars)

        self.company = make_company("Irregularities Co")
        self.shift = EmployeeShift.objects.create(employee_shift="Day Shift")
        self.monday = EmployeeShiftDay.objects.filter(day="monday").first()
        self.schedule = EmployeeShiftSchedule.objects.create(
            day=self.monday, shift_id=self.shift,
            start_time="09:00", end_time="18:00",
        )

        self.admin_user = make_user("irregularities_admin", is_superuser=True)
        self.admin = make_employee(
            company=self.company, email="irregularities_admin@test.horilla", user=self.admin_user,
        )
        self.client.force_login(self.admin_user)

        AttendanceRuleSet.objects.create(
            tier="COMPANY", company=self.company, mode=AttendanceRuleSet.MODE_FLEXIBLE,
            total_work_hours_reference="9",
        )

        self.shift_employee = make_employee(
            company=self.company, email="shift_emp@test.horilla",
            user=make_user("shift_emp"), shift=self.shift,
        )
        EmployeeWorkInformation.objects.filter(employee_id=self.shift_employee).update(
            reporting_manager_id=self.admin
        )
        self.flexible_employee = make_employee(
            company=self.company, email="flex_emp@test.horilla", user=make_user("flex_emp"),
        )
        EmployeeWorkInformation.objects.filter(employee_id=self.flexible_employee).update(
            reporting_manager_id=self.admin
        )

    def _clear_context_vars(self):
        _thread_locals.request = None
        clear_selected_company()

    def _monday_on_or_before(self, day):
        while day.weekday() != 0:
            day -= timedelta(days=1)
        return day


class PageAccessTests(AttendanceIrregularitiesTestBase):
    def test_page_list_and_nav_render(self):
        self.assertEqual(self.client.get(self.PAGE_URL).status_code, 200)
        self.assertEqual(self.client.get(self.INSTANCES_URL, **self.HX).status_code, 200)
        self.assertEqual(self.client.get(self.NAV_URL, **self.HX).status_code, 200)
        self.assertEqual(self.client.get(self.BY_EMPLOYEE_URL, **self.HX).status_code, 200)


class InstancesTests(AttendanceIrregularitiesTestBase):
    def test_late_come_row_shows_expected_actual_delta(self):
        day = self._monday_on_or_before(date.today() - timedelta(days=1))
        attendance = Attendance.objects.create(
            employee_id=self.shift_employee, attendance_date=day,
            attendance_day=self.monday, shift_id=self.shift,
            attendance_clock_in="09:20", attendance_clock_in_date=day,
            attendance_clock_out="18:00", attendance_clock_out_date=day,
        )
        AttendanceLateComeEarlyOut.objects.create(
            type="late_come", attendance_id=attendance, employee_id=self.shift_employee,
        )
        response = self.client.get(self.INSTANCES_URL, **self.HX)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "09:00")
        self.assertContains(response, "09:20")
        self.assertContains(response, "20 min")

    def test_flexible_shortfall_row_is_included_unlike_the_older_screen(self):
        day = date.today() - timedelta(days=1)
        attendance = Attendance.objects.create(
            employee_id=self.flexible_employee, attendance_date=day,
            attendance_clock_in="09:00", attendance_clock_in_date=day,
            attendance_clock_out="16:00", attendance_clock_out_date=day,
            attendance_worked_hour="07:00",
            attendance_rule_set_snapshot={
                "own": {"total_work_hours_reference": "9"}, "company_default": None,
            },
        )
        AttendanceLateComeEarlyOut.objects.create(
            type="flexible_shortfall", attendance_id=attendance, employee_id=self.flexible_employee,
        )
        response = self.client.get(self.INSTANCES_URL, **self.HX)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "9h")
        self.assertContains(response, "07:00")

    def test_a_manager_sees_only_their_own_reports(self):
        outside_employee = make_employee(
            company=self.company, email="outside@test.horilla", user=make_user("outside_emp"),
        )
        day = date.today() - timedelta(days=1)
        outside_attendance = Attendance.objects.create(
            employee_id=outside_employee, attendance_date=day,
            attendance_clock_in="09:00", attendance_clock_in_date=day,
            attendance_clock_out="16:00", attendance_clock_out_date=day,
        )
        AttendanceLateComeEarlyOut.objects.create(
            type="early_out", attendance_id=outside_attendance, employee_id=outside_employee,
        )
        manager_user = make_user("plain_manager")
        manager = make_employee(
            company=self.company, email="plain_manager@test.horilla", user=manager_user,
        )
        self.client.force_login(manager_user)
        response = self.client.get(self.INSTANCES_URL, **self.HX)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, str(outside_employee))


class ByEmployeeTests(AttendanceIrregularitiesTestBase):
    def test_counts_are_aggregated_per_employee(self):
        day = self._monday_on_or_before(date.today() - timedelta(days=1))
        attendance = Attendance.objects.create(
            employee_id=self.shift_employee, attendance_date=day,
            attendance_day=self.monday, shift_id=self.shift,
            attendance_clock_in="09:20", attendance_clock_in_date=day,
            attendance_clock_out="17:00", attendance_clock_out_date=day,
        )
        AttendanceLateComeEarlyOut.objects.create(
            type="late_come", attendance_id=attendance, employee_id=self.shift_employee,
        )
        AttendanceLateComeEarlyOut.objects.create(
            type="early_out", attendance_id=attendance, employee_id=self.shift_employee,
        )
        response = self.client.get(self.BY_EMPLOYEE_URL, **self.HX)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, str(self.shift_employee))
