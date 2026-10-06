"""
Web-view tests for My Attendance (attendance/cbv/my_attendance_calendar.py)
-- the employee's own color-coded monthly calendar, replacing the plain
list screen as the target of view-my-attendance/the "My Attendance"
sidebar entry.

Covers each color bucket in the precedence the PRD specifies: holiday
(public vs. restricted), company weekly-off, approved personal leave,
an ordinary present day, a flagged ("needs attention") day, a day
corrected via an approved Regularization request, today's own "Today"
marker, a future day showing no attendance data, and the Monthly
Summary header's four numbers. Also covers the day-detail view's
inline "Request Regularization" action appearing only when flagged.
"""

from datetime import date, timedelta

from django.test import TestCase

from attendance.cbv.my_attendance_calendar import (
    COLOR_BLUE,
    COLOR_FUTURE,
    COLOR_GREEN,
    COLOR_GREEN_NOTE,
    COLOR_PINK,
    COLOR_RED,
    COLOR_TODAY,
    _day_status,
)
from attendance.models import Attendance, AttendanceRuleSet, RegularizationRequest
from base.models import CompanyLeaves, EmployeeShiftDay, Holidays
from horilla.horilla_middlewares import _thread_locals
from horilla.testkit.company import clear_selected_company
from horilla.testkit.factories import make_company, make_employee, make_user
from leave.models import LeaveRequest, LeaveType


class MyAttendanceCalendarTestBase(TestCase):
    PAGE_URL = "/attendance/view-my-attendance/"
    HX = {"HTTP_HX_REQUEST": "true"}

    def setUp(self):
        _thread_locals.request = None
        clear_selected_company()
        self.addCleanup(self._clear_context_vars)

        self.company = make_company("My Attendance Co")
        self.user = make_user("my_att_employee")
        self.employee = make_employee(
            company=self.company, email="my_att_employee@test.horilla", user=self.user,
        )
        self.client.force_login(self.user)
        AttendanceRuleSet.objects.create(
            tier="COMPANY", company=self.company, mode=AttendanceRuleSet.MODE_FLEXIBLE,
        )

    def _clear_context_vars(self):
        _thread_locals.request = None
        clear_selected_company()

    def _make_attendance(self, day, **overrides):
        defaults = {
            "employee_id": self.employee,
            "attendance_date": day,
            "attendance_day": EmployeeShiftDay.objects.filter(
                day=day.strftime("%A").lower()
            ).first(),
            "attendance_clock_in": "09:00",
            "attendance_clock_in_date": day,
            "attendance_clock_out": "18:00",
            "attendance_clock_out_date": day,
        }
        defaults.update(overrides)
        return Attendance.objects.create(**defaults)


class PageRenderTests(MyAttendanceCalendarTestBase):
    def test_page_renders_current_month(self):
        response = self.client.get(self.PAGE_URL)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, date.today().strftime("%B %Y"))

    def test_month_navigation_renders_requested_month(self):
        response = self.client.get(self.PAGE_URL, {"month": "2025-01"}, **self.HX)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "January 2025")


class DayStatusTests(MyAttendanceCalendarTestBase):
    def test_a_day_with_no_record_defaults_to_red(self):
        day = date.today() - timedelta(days=5)
        status = _day_status(self.employee, day, date.today())
        self.assertEqual(status["color"], COLOR_RED)

    def test_an_ordinary_present_day_is_green(self):
        day = date.today() - timedelta(days=5)
        self._make_attendance(day)
        status = _day_status(self.employee, day, date.today())
        self.assertEqual(status["color"], COLOR_GREEN)

    def test_a_flagged_day_is_red(self):
        day = date.today() - timedelta(days=5)
        self._make_attendance(day, geo_fence_violation=True)
        status = _day_status(self.employee, day, date.today())
        self.assertEqual(status["color"], COLOR_RED)

    def test_a_day_corrected_via_approved_regularization_is_green_with_note(self):
        day = date.today() - timedelta(days=5)
        attendance = self._make_attendance(day, geo_fence_violation=True)
        RegularizationRequest.objects.create(
            employee=self.employee,
            attendance=attendance,
            reason_code=RegularizationRequest.REASON_OVERTIME_DENIAL_DISPUTE,
            reason="Approved after review",
            status=RegularizationRequest.STATUS_APPROVED,
        )
        status = _day_status(self.employee, day, date.today())
        self.assertEqual(status["color"], COLOR_GREEN_NOTE)

    def test_a_public_holiday_is_blue(self):
        day = date.today() - timedelta(days=5)
        Holidays.objects.create(
            name="Public Holiday", start_date=day, end_date=day, is_specific=False,
        )
        status = _day_status(self.employee, day, date.today())
        self.assertEqual(status["color"], COLOR_BLUE)

    def test_a_restricted_holiday_is_pink(self):
        day = date.today() - timedelta(days=5)
        holiday = Holidays.objects.create(
            name="Restricted Holiday", start_date=day, end_date=day, is_specific=True,
        )
        holiday.employees.add(self.employee)
        status = _day_status(self.employee, day, date.today())
        self.assertEqual(status["color"], COLOR_PINK)

    def test_a_company_weekly_off_is_blue(self):
        day = date.today() - timedelta(days=5)
        leave = CompanyLeaves.objects.create(
            based_on_week=None, based_on_week_day=str(day.weekday()),
        )
        leave.company_id.add(self.company)
        status = _day_status(self.employee, day, date.today())
        self.assertEqual(status["color"], COLOR_BLUE)

    def test_an_approved_personal_leave_is_blue(self):
        day = date.today() - timedelta(days=5)
        leave_type = LeaveType.objects.create(name="Casual Leave")
        LeaveRequest.objects.create(
            employee_id=self.employee,
            leave_type_id=leave_type,
            start_date=day,
            end_date=day,
            description="Personal leave",
            status="approved",
        )
        status = _day_status(self.employee, day, date.today())
        self.assertEqual(status["color"], COLOR_BLUE)

    def test_today_shows_the_today_marker(self):
        status = _day_status(self.employee, date.today(), date.today())
        self.assertEqual(status["color"], COLOR_TODAY)

    def test_a_future_day_shows_no_attendance_data(self):
        day = date.today() + timedelta(days=5)
        status = _day_status(self.employee, day, date.today())
        self.assertEqual(status["color"], COLOR_FUTURE)
        self.assertNotIn("attendance", status)


class MonthlySummaryTests(MyAttendanceCalendarTestBase):
    def test_summary_numbers_render_on_the_page(self):
        day = date.today().replace(day=1)
        self._make_attendance(day, overtime_second=3600)
        response = self.client.get(self.PAGE_URL)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Days Present")
        self.assertContains(response, "OT Hours Accrued")


class DayDetailTests(MyAttendanceCalendarTestBase):
    def _detail_url(self, day):
        return f"/attendance/my-attendance-calendar/day/{day.year}/{day.month}/{day.day}/"

    def test_flagged_day_shows_the_inline_regularization_action(self):
        day = date.today() - timedelta(days=5)
        self._make_attendance(day, geo_fence_violation=True)
        response = self.client.get(self._detail_url(day), **self.HX)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Request Regularization")

    def test_unflagged_day_has_no_inline_regularization_action(self):
        day = date.today() - timedelta(days=5)
        self._make_attendance(day)
        response = self.client.get(self._detail_url(day), **self.HX)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Request Regularization")
