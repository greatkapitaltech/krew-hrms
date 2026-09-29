"""
base/caching.py

Read-mostly, hot-path lookups that live in base/ because the underlying
models do (Holidays, CompanyLeaves) or because base itself is a direct
caller (AttendanceGeneralSetting -- base/context_processors.py and
base/templatetags/horillafilters.py both query it directly today,
uncached; not yet switched over to the cached version here, see that
decision's own note below). All three change only when an admin
deliberately edits a setting, so there's no reason to hit the database
for them on every call. TTL is settings.CACHE_TTL_SECONDS
(horilla/settings/base.py, env-overridable).

Originally built in attendance/caching.py (Part 3 of the Attendance
performance plan); moved here once it turned up that Holidays/
CompanyLeaves are used across leave/, horilla_api/, and base/ itself --
not attendance-specific at all -- and that AttendanceGeneralSetting has
its own uncached, cross-app callers in base/ that are hotter than the
attendance code this was originally written for (a context processor
runs on every page render). shift_schedule_today()'s cache stayed behind
in attendance/caching.py -- EmployeeShiftSchedule has no callers outside
attendance/.

Invalidation is a version counter, not direct key deletion, for all
three: one Holidays row can affect many (date, employee) cache keys at
once (a date range, or every year forever if recurring=True); one
AttendanceGeneralSetting bulk .update() (attendance/views/views.py's
enable_timerunner()/enable_disable_check_in()) can affect many rows at
once too and, being a bulk QuerySet.update(), never fires post_save in
the first place -- callers doing that kind of write must call
bust_attendance_general_settings_cache() explicitly (see below).
"""

from django.apps import apps
from django.conf import settings
from django.core.cache import cache
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from base.models import CompanyLeaves, Holidays
from horilla.horilla_middlewares import get_selected_company

_MISSING = object()


def _cache_version_key(namespace):
    return f"cache_version:{namespace}"


def _current_cache_version(namespace):
    key = _cache_version_key(namespace)
    version = cache.get(key)
    if version is None:
        version = 0
        cache.set(key, version, None)
    return version


def _bump_cache_version(namespace):
    key = _cache_version_key(namespace)
    try:
        cache.incr(key)
    except ValueError:
        cache.set(key, 1, None)


# ---------------------------------------------------------------------------
# AttendanceGeneralSetting
# ---------------------------------------------------------------------------
# The model itself lives in attendance/models.py; referenced lazily here
# (rather than a top-level `from attendance.models import
# AttendanceGeneralSetting`) so base/ doesn't hard-depend on attendance/
# being installed -- same guard base/context_processors.py's
# timerunner_enabled() already uses for this exact model.


def _attendance_general_setting_model():
    if not apps.is_installed("attendance"):
        return None
    return apps.get_model("attendance", "AttendanceGeneralSetting")


def _general_settings_cache_key(company_id):
    version = _current_cache_version("attendance_general_settings")
    return f"attendance_general_settings:{company_id}:{version}"


def get_cached_attendance_general_settings(company):
    """
    AttendanceGeneralSetting.objects.filter(company_id=company).first(),
    cached. `company` may be None (the global "all companies" row).
    Returns None if the attendance app isn't installed.
    """
    model = _attendance_general_setting_model()
    if model is None:
        return None

    company_id = getattr(company, "pk", None)
    cache_key = _general_settings_cache_key(company_id)
    cached = cache.get(cache_key, _MISSING)
    if cached is not _MISSING:
        return cached

    setting = model.objects.filter(company_id=company).first()
    cache.set(cache_key, setting, settings.CACHE_TTL_SECONDS)
    return setting


def bust_attendance_general_settings_cache():
    """
    For call sites that mutate AttendanceGeneralSetting via a bulk
    QuerySet.update() -- which never fires post_save -- rather than
    .save(): attendance/views/views.py's enable_timerunner() and
    enable_disable_check_in() both do this today and call this
    explicitly right after. A plain .save() elsewhere doesn't need this;
    the signal receiver below already covers that path.
    """
    _bump_cache_version("attendance_general_settings")


def register_attendance_general_setting_signals():
    """
    Deliberately NOT called automatically at module import time (unlike
    the Holidays/CompanyLeaves @receiver decorators below, which are
    safe to run eagerly since base.models has no dependency on
    attendance.models). attendance/models.py imports get_cached_is_holiday
    et al from this module at its own top level -- if this function ran
    eagerly on import, and something imports base.caching *from within*
    attendance/models.py's own top-level import block, apps.get_model()
    would resolve AttendanceGeneralSetting before that class is even
    defined later in that same file. base/apps.py's ready() calls this
    explicitly instead, once every app's models are guaranteed fully
    loaded.
    """
    model = _attendance_general_setting_model()
    if model is None:
        return

    def _bust(sender, instance, **kwargs):
        bust_attendance_general_settings_cache()

    post_save.connect(_bust, sender=model, weak=False)
    post_delete.connect(_bust, sender=model, weak=False)


# ---------------------------------------------------------------------------
# Holidays
# ---------------------------------------------------------------------------


def _is_holiday_cache_key(check_date, employee_id):
    # Holidays.objects is itself HorillaCompanyManager-scoped -- its
    # result already implicitly depends on get_selected_company() at
    # call time, not just the explicit date/employee args, so that has
    # to be part of the key too or a cached answer from one company
    # could leak into another's.
    company_id = get_selected_company()
    version = _current_cache_version("is_holiday")
    return f"is_holiday:{check_date.isoformat()}:{employee_id}:{company_id}:{version}"


def get_cached_is_holiday(check_date, employee=None):
    """
    base.methods.is_holiday(), cached. Returns the same thing it does:
    the Holidays instance if check_date is a holiday, else False.
    """
    from base.methods import is_holiday

    cache_key = _is_holiday_cache_key(check_date, getattr(employee, "pk", None))
    cached = cache.get(cache_key, _MISSING)
    if cached is not _MISSING:
        return cached

    result = is_holiday(check_date, employee)
    cache.set(cache_key, result, settings.CACHE_TTL_SECONDS)
    return result


def bust_is_holiday_cache():
    """
    For call sites that create Holidays via bulk_create() -- which never
    fires post_save -- rather than .save(): base/views.py's CSV/Excel
    holiday-import handlers do this today and call this explicitly right
    after. A plain .save()/.create() elsewhere doesn't need this; the
    signal receiver below already covers that path.
    """
    _bump_cache_version("is_holiday")


@receiver(post_save, sender=Holidays)
@receiver(post_delete, sender=Holidays)
def _bust_is_holiday_cache_signal(sender, instance, **kwargs):
    # A single Holidays row can affect many (date, employee) combinations
    # at once -- a multi-day range, or (recurring=True) every year on
    # that month/day forever -- so there's no fixed set of keys to
    # delete directly; bump the shared version instead (see module
    # docstring).
    bust_is_holiday_cache()


# ---------------------------------------------------------------------------
# CompanyLeaves
# ---------------------------------------------------------------------------


def _is_company_leave_cache_key(check_date):
    company_id = get_selected_company()
    version = _current_cache_version("is_company_leave")
    return f"is_company_leave:{check_date.isoformat()}:{company_id}:{version}"


def get_cached_is_company_leave(check_date):
    """
    base.methods.is_company_leave(), cached. Returns the same thing it
    does: the CompanyLeaves instance if check_date matches a configured
    weekly-off pattern, else False.
    """
    from base.methods import is_company_leave

    cache_key = _is_company_leave_cache_key(check_date)
    cached = cache.get(cache_key, _MISSING)
    if cached is not _MISSING:
        return cached

    result = is_company_leave(check_date)
    cache.set(cache_key, result, settings.CACHE_TTL_SECONDS)
    return result


@receiver(post_save, sender=CompanyLeaves)
@receiver(post_delete, sender=CompanyLeaves)
def _bust_is_company_leave_cache(sender, instance, **kwargs):
    # Same reasoning as Holidays above -- a recurring weekly pattern
    # affects every matching weekday forever, not one fixed date.
    _bump_cache_version("is_company_leave")
