"""
Tests for Geo-tag/Geo-mark persistence wired into clock_in_out.py:
- clock_in_attendance_and_activity() / clock_out_attendance_and_activity()
  persist submitted coordinates onto AttendanceActivity (separate clock-in
  vs clock-out fields, since a session's two punches can legitimately
  happen at different locations)
- geo_fence_violation / geo_fence_unverified are OR'd onto Attendance,
  never overwritten back to False by a later, clean punch
"""

from datetime import date, datetime, time

from django.test import TestCase
from django.utils import timezone

from attendance.models import Attendance, AttendanceActivity
from attendance.views.clock_in_out import (
    clock_in_attendance_and_activity,
    clock_out_attendance_and_activity,
)
from base.models import EmployeeShift, EmployeeShiftDay, EmployeeShiftSchedule
from horilla.testkit.factories import make_company, make_employee, make_user


class GeoTaggingPersistenceTests(TestCase):
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
            company=self.company, email="geotag@test.horilla",
            user=make_user("geotag"), shift=self.shift,
        )

    def _clock_in(self, **kwargs):
        defaults = dict(
            employee=self.employee,
            date_today=date(2026, 9, 21),  # a Monday
            attendance_date=date(2026, 9, 21),
            day=self.day,
            now="09:05",
            shift=self.shift,
            minimum_hour="08:00",
            start_time=32400,
            end_time=61200,
            in_datetime=timezone.make_aware(datetime(2026, 9, 21, 9, 5)),
        )
        defaults.update(kwargs)
        return clock_in_attendance_and_activity(**defaults)

    def _clock_out(self, **kwargs):
        defaults = dict(
            employee=self.employee,
            date_today=date(2026, 9, 21),
            now="17:05",
            out_datetime=timezone.make_aware(datetime(2026, 9, 21, 17, 5)),
        )
        defaults.update(kwargs)
        return clock_out_attendance_and_activity(**defaults)

    def test_clock_in_persists_coordinates_on_the_activity_row(self):
        attendance = self._clock_in(latitude=12.9716, longitude=77.5946)
        activity = AttendanceActivity.objects.get(
            employee_id=self.employee, attendance_date=attendance.attendance_date,
        )
        self.assertEqual(activity.clock_in_latitude, 12.9716)
        self.assertEqual(activity.clock_in_longitude, 77.5946)
        self.assertIsNone(activity.clock_out_latitude)

    def test_clock_out_persists_coordinates_separately_from_clock_in(self):
        self._clock_in(latitude=12.9716, longitude=77.5946)
        self._clock_out(latitude=13.0, longitude=78.0)
        activity = AttendanceActivity.objects.get(employee_id=self.employee)
        self.assertEqual(activity.clock_in_latitude, 12.9716)
        self.assertEqual(activity.clock_out_latitude, 13.0)
        self.assertEqual(activity.clock_out_longitude, 78.0)

    def test_web_punch_leaves_coordinates_null(self):
        attendance = self._clock_in()
        activity = AttendanceActivity.objects.get(
            employee_id=self.employee, attendance_date=attendance.attendance_date,
        )
        self.assertIsNone(activity.clock_in_latitude)
        self.assertIsNone(activity.clock_in_longitude)

    def test_violation_flag_set_at_clock_in_persists_on_attendance(self):
        attendance = self._clock_in(geo_fence_violation=True)
        attendance.refresh_from_db()
        self.assertTrue(attendance.geo_fence_violation)
        self.assertFalse(attendance.geo_fence_unverified)

    def test_unverified_flag_set_at_clock_out_persists_on_attendance(self):
        self._clock_in()
        attendance = self._clock_out(geo_fence_unverified=True)
        attendance.refresh_from_db()
        self.assertTrue(attendance.geo_fence_unverified)
        self.assertFalse(attendance.geo_fence_violation)

    def test_flag_is_ored_not_overwritten_on_reclock_in(self):
        attendance = self._clock_in(geo_fence_violation=True)
        self.assertTrue(attendance.geo_fence_violation)

        # Employee clocks out then back in again the same day, this time
        # cleanly (no violation) -- the earlier flag must survive.
        self._clock_out()
        attendance_again = self._clock_in(geo_fence_violation=False)
        self.assertTrue(attendance_again.geo_fence_violation)

    def test_no_flags_by_default(self):
        attendance = self._clock_in()
        self.assertFalse(attendance.geo_fence_violation)
        self.assertFalse(attendance.geo_fence_unverified)
