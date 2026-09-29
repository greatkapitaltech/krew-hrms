"""
attendance/caching.py

The shift-schedule lookup on the clock-in/clock-out hot path (Part 3 Sec
3.3 of the Attendance performance plan) -- changes only when an admin
deliberately edits a schedule, so there's no reason to hit the database
for it on every single punch. Invalidated by direct key deletion on
save/delete: EmployeeShiftSchedule maps one row to exactly one cache key
((day_id, shift_id)), so there's nothing a version counter would buy
here (contrast base/caching.py's Holidays/CompanyLeaves caches, where
one row *can* be the answer for many keys at once).

AttendanceGeneralSetting/Holidays/CompanyLeaves caching used to live
here too; moved to base/caching.py once it turned up that all three have
real callers outside attendance/ -- Holidays/CompanyLeaves across
leave/, horilla_api/, and base/ itself, and AttendanceGeneralSetting
queried directly (uncached) by base/context_processors.py's
timerunner_enabled(), a context processor that runs on every page
render, not just attendance's own hot path.
"""

from django.conf import settings
from django.core.cache import cache
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from base.models import EmployeeShiftSchedule

_MISSING = object()


def _shift_schedule_cache_key(day_id, shift_id):
    return f"shift_schedule_today:{day_id}:{shift_id}"


def get_cached_shift_schedule_row(day, shift):
    """
    The single EmployeeShiftSchedule row for (day, shift), cached -- the
    query shift_schedule_today() (attendance/methods/utils.py) builds its
    (minimum_hour, start_time_sec, end_time_sec) tuple from. Returns None
    if no schedule is configured for that day/shift, matching the
    underlying queryset's `.first()`.
    """
    cache_key = _shift_schedule_cache_key(day.pk, shift.pk if shift else None)
    cached = cache.get(cache_key, _MISSING)
    if cached is not _MISSING:
        return cached

    schedule = day.day_schedule.filter(shift_id=shift).first()
    cache.set(cache_key, schedule, settings.CACHE_TTL_SECONDS)
    return schedule


@receiver(post_save, sender=EmployeeShiftSchedule)
@receiver(post_delete, sender=EmployeeShiftSchedule)
def _bust_shift_schedule_cache(sender, instance, **kwargs):
    cache.delete(_shift_schedule_cache_key(instance.day_id, instance.shift_id_id))
