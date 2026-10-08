"""
Caching for AttendanceGeneralSetting/is_holiday()/is_company_leave()
(base/caching.py). Originally built in attendance/caching.py (Part 3 of
the Attendance performance plan) and moved here once it turned up that
all three have real callers outside attendance/ -- Holidays/
CompanyLeaves across leave/, horilla_api/, and base/ itself, and
AttendanceGeneralSetting queried directly (uncached) by
base/context_processors.py's timerunner_enabled(), a context processor
that runs on every page render. shift_schedule_today()'s cache stayed in
attendance/caching.py -- EmployeeShiftSchedule has no callers outside
attendance/ (see attendance/tests/test_hot_path_caching.py).
"""

from datetime import date

from django.core.cache import cache
from django.test import TestCase

from base.caching import (
    get_cached_attendance_general_settings,
    get_cached_is_company_leave,
    get_cached_is_holiday,
)
from base.models import CompanyLeaves, Holidays
from horilla.horilla_middlewares import _thread_locals
from horilla.testkit.company import clear_selected_company
from horilla.testkit.factories import make_company, make_employee, make_user

try:
    from attendance.models import AttendanceGeneralSetting
except ImportError:  # attendance not installed -- see base/caching.py's guard
    AttendanceGeneralSetting = None


class CacheTestBase(TestCase):
    def setUp(self):
        _thread_locals.request = None
        clear_selected_company()
        cache.clear()
        self.addCleanup(self._clear_context_vars)

    @staticmethod
    def _clear_context_vars():
        _thread_locals.request = None
        clear_selected_company()


class AttendanceGeneralSettingCacheTests(CacheTestBase):
    def setUp(self):
        super().setUp()
        # attendance/signals.py's create_attendance_setting() auto-creates
        # one AttendanceGeneralSetting per company (and one company_id=None
        # global row) the moment the company itself is saved -- there's no
        # "genuinely missing" case to test against in practice, so these
        # tests work with that auto-created row rather than creating a
        # second, redundant one (company_id isn't unique on this model,
        # so a second .create() wouldn't collide -- it'd just make the
        # cached "first()" pick nondeterministically between two rows).
        self.company = make_company("General Setting Cache Co")
        self.setting = AttendanceGeneralSetting.objects.get(company_id=self.company)

    def test_returns_the_configured_row_and_caches_it(self):
        first = get_cached_attendance_general_settings(self.company)
        second = get_cached_attendance_general_settings(self.company)
        self.assertEqual(first.pk, self.setting.pk)
        self.assertEqual(second.pk, self.setting.pk)

    def test_editing_the_setting_invalidates_the_cache(self):
        get_cached_attendance_general_settings(self.company)  # warm the cache

        self.setting.enable_check_in = False
        self.setting.save()

        resolved = get_cached_attendance_general_settings(self.company)
        self.assertFalse(resolved.enable_check_in)

    def test_deleting_the_setting_invalidates_the_cache(self):
        get_cached_attendance_general_settings(self.company)  # warm the cache
        self.setting.delete()

        self.assertIsNone(get_cached_attendance_general_settings(self.company))

    def test_none_company_global_row_is_its_own_cache_entry(self):
        self.setting.enable_check_in = False
        self.setting.save()
        global_setting = AttendanceGeneralSetting.objects.get(company_id=None)
        global_setting.enable_check_in = True
        global_setting.save()

        company_result = get_cached_attendance_general_settings(self.company)
        global_result = get_cached_attendance_general_settings(None)

        self.assertFalse(company_result.enable_check_in)
        self.assertEqual(global_result.pk, global_setting.pk)
        self.assertTrue(global_result.enable_check_in)

    def test_a_bulk_update_is_reflected_not_a_stale_cache(self):
        # AttendanceGeneralSetting.objects.filter(...).update(...) --
        # what attendance/views/views.py's enable_timerunner()/
        # enable_disable_check_in() actually do -- bypasses post_save
        # entirely; this only works because those call sites explicitly
        # call bust_attendance_general_settings_cache() afterward.
        get_cached_attendance_general_settings(self.company)  # warm the cache

        from base.caching import bust_attendance_general_settings_cache

        AttendanceGeneralSetting.objects.filter(pk=self.setting.pk).update(
            enable_check_in=False
        )
        bust_attendance_general_settings_cache()

        resolved = get_cached_attendance_general_settings(self.company)
        self.assertFalse(resolved.enable_check_in)


class IsHolidayCacheTests(CacheTestBase):
    def setUp(self):
        super().setUp()
        self.company = make_company("Holiday Cache Co")
        self.employee = make_employee(
            company=self.company, email="holiday_cache@test.horilla",
            user=make_user("holiday_cache_emp"),
        )

    def test_no_holiday_configured_returns_false_and_caches_it(self):
        self.assertFalse(get_cached_is_holiday(date(2026, 9, 21), self.employee))
        self.assertFalse(get_cached_is_holiday(date(2026, 9, 21), self.employee))

    def test_returns_the_matching_holiday_and_caches_it(self):
        holiday = Holidays.objects.create(
            name="Founders Day", start_date=date(2026, 9, 21),
            end_date=date(2026, 9, 21), company_id=self.company,
        )
        first = get_cached_is_holiday(date(2026, 9, 21), self.employee)
        second = get_cached_is_holiday(date(2026, 9, 21), self.employee)
        self.assertEqual(first.pk, holiday.pk)
        self.assertEqual(second.pk, holiday.pk)

    def test_a_newly_added_holiday_is_picked_up_not_a_stale_false(self):
        # Warm the cache on "not a holiday" first.
        self.assertFalse(get_cached_is_holiday(date(2026, 9, 21), self.employee))

        holiday = Holidays.objects.create(
            name="Founders Day", start_date=date(2026, 9, 21),
            end_date=date(2026, 9, 21), company_id=self.company,
        )

        resolved = get_cached_is_holiday(date(2026, 9, 21), self.employee)
        self.assertEqual(resolved.pk, holiday.pk)

    def test_deleting_a_holiday_is_reflected_on_the_next_call(self):
        holiday = Holidays.objects.create(
            name="Founders Day", start_date=date(2026, 9, 21),
            end_date=date(2026, 9, 21), company_id=self.company,
        )
        get_cached_is_holiday(date(2026, 9, 21), self.employee)  # warm the cache
        holiday.delete()

        self.assertFalse(get_cached_is_holiday(date(2026, 9, 21), self.employee))

    def test_a_recurring_holiday_matches_every_year_and_stays_cached_correctly(self):
        Holidays.objects.create(
            name="Republic Day", start_date=date(2020, 1, 26), recurring=True,
            company_id=self.company,
        )
        self.assertTrue(get_cached_is_holiday(date(2026, 1, 26), self.employee))
        self.assertTrue(get_cached_is_holiday(date(2027, 1, 26), self.employee))
        self.assertFalse(get_cached_is_holiday(date(2026, 1, 27), self.employee))

    def test_a_bulk_created_holiday_is_reflected_not_a_stale_cache(self):
        # bulk_create() -- what base/views.py's CSV/Excel holiday-import
        # handlers actually use -- bypasses post_save entirely; this
        # only works because those call sites explicitly call
        # bust_is_holiday_cache() afterward.
        self.assertFalse(get_cached_is_holiday(date(2026, 9, 21), self.employee))

        from base.caching import bust_is_holiday_cache

        created = Holidays.objects.bulk_create([
            Holidays(
                name="Bulk Imported Day", start_date=date(2026, 9, 21),
                end_date=date(2026, 9, 21), company_id=self.company,
            )
        ])
        bust_is_holiday_cache()

        resolved = get_cached_is_holiday(date(2026, 9, 21), self.employee)
        self.assertEqual(resolved.pk, created[0].pk)


class IsCompanyLeaveCacheTests(CacheTestBase):
    def setUp(self):
        super().setUp()
        self.company = make_company("Leave Cache Co")

    def test_no_leave_pattern_configured_returns_false_and_caches_it(self):
        # A Sunday with no configured weekly-off pattern.
        self.assertFalse(get_cached_is_company_leave(date(2026, 9, 20)))
        self.assertFalse(get_cached_is_company_leave(date(2026, 9, 20)))

    def test_returns_the_matching_weekly_off_and_caches_it(self):
        leave = CompanyLeaves.objects.create(based_on_week=None, based_on_week_day="6")
        leave.company_id.add(self.company)

        first = get_cached_is_company_leave(date(2026, 9, 20))  # a Sunday
        second = get_cached_is_company_leave(date(2026, 9, 20))
        self.assertEqual(first.pk, leave.pk)
        self.assertEqual(second.pk, leave.pk)

    def test_a_newly_added_pattern_is_picked_up_not_a_stale_false(self):
        self.assertFalse(get_cached_is_company_leave(date(2026, 9, 20)))

        leave = CompanyLeaves.objects.create(based_on_week=None, based_on_week_day="6")
        leave.company_id.add(self.company)

        resolved = get_cached_is_company_leave(date(2026, 9, 20))
        self.assertEqual(resolved.pk, leave.pk)

    def test_deleting_a_pattern_is_reflected_on_the_next_call(self):
        leave = CompanyLeaves.objects.create(based_on_week=None, based_on_week_day="6")
        leave.company_id.add(self.company)
        get_cached_is_company_leave(date(2026, 9, 20))  # warm the cache
        leave.delete()

        self.assertFalse(get_cached_is_company_leave(date(2026, 9, 20)))
