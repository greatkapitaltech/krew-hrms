"""
Web-view tests for My Krew Attendance (attendance/cbv/my_krew_attendance.py)
-- the manager/HR-facing dashboard unifying Validation, Overtime,
Regularization, and Attendance Irregularities, plus a per-employee
calendar reusing My Attendance's own day-status computation.
"""

from datetime import date, timedelta

from django.test import TestCase

from attendance.models import Attendance, RegularizationRequest
from employee.models import EmployeeWorkInformation
from horilla.horilla_middlewares import _thread_locals
from horilla.testkit.company import clear_selected_company
from horilla.testkit.factories import make_company, make_employee, make_user


class MyKrewAttendanceTestBase(TestCase):
    PAGE_URL = "/attendance/my-krew-attendance/"
    VIEW_URL = "/attendance/my-krew-attendance/view/"
    ACTION_URL = "/attendance/my-krew-attendance/need-your-action/"
    HX = {"HTTP_HX_REQUEST": "true"}

    def setUp(self):
        _thread_locals.request = None
        clear_selected_company()
        self.addCleanup(self._clear_context_vars)

        self.company = make_company("My Krew Co")
        self.manager_user = make_user("krew_manager")
        self.manager = make_employee(
            company=self.company, email="krew_manager@test.horilla", user=self.manager_user,
        )
        self.employee = make_employee(
            company=self.company, email="krew_employee@test.horilla", user=make_user("krew_employee"),
        )
        EmployeeWorkInformation.objects.filter(employee_id=self.employee).update(
            reporting_manager_id=self.manager
        )
        self.client.force_login(self.manager_user)

    def _clear_context_vars(self):
        _thread_locals.request = None
        clear_selected_company()

    def _calendar_url(self, employee):
        today = date.today()
        return (
            f"/attendance/my-krew-attendance/employee/{employee.pk}/calendar/"
            f"?year={today.year}&month={today.month}"
        )


class PageAccessTests(MyKrewAttendanceTestBase):
    def test_page_and_tabs_render(self):
        self.assertEqual(self.client.get(self.PAGE_URL).status_code, 200)
        self.assertEqual(self.client.get(self.VIEW_URL, **self.HX).status_code, 200)
        self.assertEqual(self.client.get(self.ACTION_URL, **self.HX).status_code, 200)


class MyKrewViewTests(MyKrewAttendanceTestBase):
    def test_dashboard_shows_team_size_and_employee_row(self):
        response = self.client.get(self.VIEW_URL, **self.HX)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, str(self.employee))
        self.assertContains(response, "Total Employees")

    def test_action_items_sums_validation_overtime_regularization(self):
        day = date.today() - timedelta(days=1)
        Attendance.objects.create(
            employee_id=self.employee, attendance_date=day,
            attendance_clock_in="09:00", attendance_clock_in_date=day,
            attendance_clock_out="18:00", attendance_clock_out_date=day,
            is_validate_request=True,
        )
        another_day = day - timedelta(days=1)
        attendance_for_request = Attendance.objects.create(
            employee_id=self.employee, attendance_date=another_day,
            attendance_clock_in="09:00", attendance_clock_in_date=another_day,
            attendance_clock_out="18:00", attendance_clock_out_date=another_day,
        )
        RegularizationRequest.objects.create(
            employee=self.employee, attendance=attendance_for_request,
            reason_code=RegularizationRequest.REASON_TIME_CORRECTION,
            reason="Forgot to punch out on time",
        )
        response = self.client.get(self.VIEW_URL, **self.HX)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, ">2<")

    def test_employee_calendar_renders_for_a_team_member(self):
        response = self.client.get(self._calendar_url(self.employee), **self.HX)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, str(self.employee))

    def test_employee_calendar_blocked_for_someone_outside_the_team(self):
        outsider = make_employee(
            company=self.company, email="krew_outsider@test.horilla", user=make_user("krew_outsider"),
        )
        response = self.client.get(self._calendar_url(outsider), **self.HX)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "not in your team")


class NeedYourActionTests(MyKrewAttendanceTestBase):
    def test_all_four_sections_render(self):
        response = self.client.get(self.ACTION_URL, **self.HX)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Validation")
        self.assertContains(response, "Overtime")
        self.assertContains(response, "Regularization")
        self.assertContains(response, "Attendance Irregularities")
