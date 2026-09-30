"""
Caching for the shift-schedule lookup on the clock-in/clock-out hot path
-- Part 3 Sec 3.3 of the Attendance performance plan
(attendance/caching.py). AttendanceGeneralSetting/is_holiday()/
is_company_leave() caching used to live alongside this in the same
module; moved to base/caching.py (see base/tests/test_caching.py) once
it turned up they have real callers outside attendance/.
EmployeeShiftSchedule has no such callers, so its cache stayed here.
"""

from datetime import time

from django.core.cache import cache
from django.test import TestCase

from attendance.caching import get_cached_shift_schedule_row
from base.models import EmployeeShift, EmployeeShiftDay, EmployeeShiftSchedule


class ShiftScheduleCacheTests(TestCase):
    def setUp(self):
        cache.clear()
        self.shift = EmployeeShift.objects.create(employee_shift="Cache Test Shift")
        self.day = EmployeeShiftDay.objects.create(day="monday")

    def test_no_schedule_configured_returns_none_and_caches_it(self):
        self.assertIsNone(get_cached_shift_schedule_row(self.day, self.shift))
        self.assertIsNone(get_cached_shift_schedule_row(self.day, self.shift))

    def test_returns_the_configured_schedule_and_caches_it(self):
        schedule = EmployeeShiftSchedule.objects.create(
            day=self.day, shift_id=self.shift,
            minimum_working_hour="08:00",
            start_time=time(9, 0), end_time=time(17, 0),
        )
        first = get_cached_shift_schedule_row(self.day, self.shift)
        second = get_cached_shift_schedule_row(self.day, self.shift)
        self.assertEqual(first.pk, schedule.pk)
        self.assertEqual(second.pk, schedule.pk)

    def test_editing_the_schedule_invalidates_the_cache(self):
        schedule = EmployeeShiftSchedule.objects.create(
            day=self.day, shift_id=self.shift,
            minimum_working_hour="08:00",
            start_time=time(9, 0), end_time=time(17, 0),
        )
        get_cached_shift_schedule_row(self.day, self.shift)  # warm the cache

        schedule.start_time = time(10, 0)
        schedule.save()

        resolved = get_cached_shift_schedule_row(self.day, self.shift)
        self.assertEqual(resolved.start_time, time(10, 0))

    def test_a_different_shift_on_the_same_day_is_a_separate_cache_entry(self):
        other_shift = EmployeeShift.objects.create(employee_shift="Other Cache Test Shift")
        schedule = EmployeeShiftSchedule.objects.create(
            day=self.day, shift_id=self.shift,
            minimum_working_hour="08:00",
            start_time=time(9, 0), end_time=time(17, 0),
        )

        self.assertEqual(
            get_cached_shift_schedule_row(self.day, self.shift).pk, schedule.pk
        )
        self.assertIsNone(get_cached_shift_schedule_row(self.day, other_shift))
