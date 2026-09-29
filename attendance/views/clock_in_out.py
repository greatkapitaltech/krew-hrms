"""
clock_in_out.py

This module is used register endpoints to the check-in check-out functionalities
"""

import logging

from django.shortcuts import render

from horilla.http.response import HorillaRedirect

logger = logging.getLogger(__name__)
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.contrib import messages
from django.db.models import Q
from django.http import HttpResponse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from attendance.methods.utils import (
    activity_datetime,
    employee_exists,
    format_time,
    shift_schedule_today,
    strtime_seconds,
)
from attendance.models import (
    Attendance,
    AttendanceActivity,
    AttendanceLateComeEarlyOut,
    AttendanceRuleSet,
    GraceTime,
)
from attendance.views.views import attendance_validate
from base.caching import get_cached_attendance_general_settings
from base.context_processors import (
    enable_late_come_early_out_tracking,
    timerunner_enabled,
)
from base.models import Company, EmployeeShiftDay
from horilla.decorators import hx_request_required, login_required
from horilla.horilla_middlewares import _thread_locals


def _enqueue_late_come_early_out(attendance):
    """
    Defers late-come/early-out/flexible-shortfall flagging to a
    background task (Part 3 of the Attendance performance plan) --
    every call site that used to call late_come()/early_out()/
    flexible_shortfall() directly enqueues this instead; the task itself
    (attendance/tasks.py) re-derives which of those three applies. Wrapped
    defensively: a broker hiccup here must never affect the punch
    response that already succeeded above this call -- the periodic
    sweep (attendance/tasks.py::sweep_stuck_background_tasks) still finds
    and processes the durable BackgroundAttendanceTask row this creates
    even if the immediate .delay() itself fails to reach a worker.
    """
    from attendance.models import BackgroundAttendanceTask
    from attendance.tasks import enqueue_background_attendance_task

    try:
        enqueue_background_attendance_task(
            attendance, BackgroundAttendanceTask.KIND_LATE_COME_EARLY_OUT,
        )
    except Exception:
        logger.exception(
            "Failed to enqueue late-come/early-out task for attendance %s",
            attendance.pk,
        )


def late_come_create(attendance):
    """
    used to create late come report
    args:
        attendance : attendance object
    """

    if AttendanceLateComeEarlyOut.objects.filter(
        type="late_come", attendance_id=attendance
    ).exists():
        late_come_obj = AttendanceLateComeEarlyOut.objects.filter(
            type="late_come", attendance_id=attendance
        ).first()
    else:
        late_come_obj = AttendanceLateComeEarlyOut()

    late_come_obj.type = "late_come"
    late_come_obj.attendance_id = attendance
    late_come_obj.employee_id = attendance.employee_id
    late_come_obj.save()
    return late_come_obj


def late_come(attendance, start_time, end_time, shift):
    """
    this method is used to mark the late check-in  attendance after the shift starts
    args:
        attendance : attendance obj
        start_time : attendance day shift start time
        end_time : attendance day shift end time

    """
    if not shift:
        return
    if not enable_late_come_early_out_tracking(None).get("tracking"):
        return
    request = getattr(_thread_locals, "request", None)
    clock_in_time = attendance.attendance_clock_in
    if isinstance(clock_in_time, str):
        # A freshly-assigned, not-yet-saved Attendance keeps whatever raw
        # "%H:%M" string was assigned to this TimeField in memory --
        # Django only coerces it to a real time on the way to the DB, not
        # on attribute assignment. Callers driven by a fresh DB read (the
        # other late_come() call sites) never hit this; the clock-in path
        # does, since it calls this on the same in-memory object it just
        # saved rather than re-fetching.
        clock_in_time = datetime.strptime(clock_in_time, "%H:%M").time()
    now_sec = strtime_seconds(clock_in_time.strftime("%H:%M"))
    mid_day_sec = strtime_seconds("12:00")

    # Checking gracetime allowance before creating late come
    if shift and shift.grace_time_id:
        # checking grace time in shift, it has the higher priority
        if (
            shift.grace_time_id.is_active == True
            and shift.grace_time_id.allowed_clock_in == True
        ):
            # Setting allowance for the check in time
            now_sec -= shift.grace_time_id.allowed_time_in_secs
    # checking default grace time
    elif GraceTime.objects.filter(is_default=True, is_active=True).exists():
        grace_time = GraceTime.objects.filter(
            is_default=True,
            is_active=True,
        ).first()
        # Setting allowance for the check in time if grace allocate for clock in event
        if grace_time.allowed_clock_in:
            now_sec -= grace_time.allowed_time_in_secs
    else:
        pass
    if start_time > end_time and start_time != end_time:
        # night shift
        if now_sec < mid_day_sec:
            # Here  attendance or attendance activity for new day night shift
            late_come_create(attendance)
        elif now_sec > start_time:
            # Here  attendance or attendance activity for previous day night shift
            late_come_create(attendance)
    elif start_time < now_sec:
        late_come_create(attendance)
    return True


def clock_in_attendance_and_activity(
    employee,
    date_today,
    attendance_date,
    day,
    now,
    shift,
    minimum_hour,
    start_time,
    end_time,
    in_datetime,
    latitude=None,
    longitude=None,
    geo_fence_violation=False,
    geo_fence_unverified=False,
):
    """
    This method is used to create attendance activity or attendance when an employee clocks-in
    args:
        employee        : employee instance
        date_today      : date
        attendance_date : the date that attendance for
        day             : shift day
        now             : current time
        shift           : shift object
        minimum_hour    : minimum hour in shift schedule
        start_time      : start time in shift schedule
        end_time        : end time in shift schedule
        latitude, longitude       : Geo-tag coordinates from a mobile punch
                                     (None for a web punch -- never required
                                     there)
        geo_fence_violation,
        geo_fence_unverified       : this punch's Geo-mark check outcome,
                                      already decided by the caller (see
                                      geofencing.methods.check_geo_fence) --
                                      OR'd onto the Attendance row, never
                                      overwriting an existing True with False
    """

    # attendance activity create
    activity = AttendanceActivity.objects.filter(
        employee_id=employee,
        attendance_date=attendance_date,
        clock_in_date=date_today,
        shift_day=day,
        clock_out=None,
    ).first()

    if activity and not activity.clock_out:
        activity.clock_out = in_datetime
        activity.clock_out_date = date_today
        activity.save()

    new_activity = AttendanceActivity.objects.create(
        employee_id=employee,
        attendance_date=attendance_date,
        clock_in_date=date_today,
        shift_day=day,
        clock_in=in_datetime,
        in_datetime=in_datetime,
        clock_in_latitude=latitude,
        clock_in_longitude=longitude,
    )
    # create attendance if not exist
    attendance = Attendance.objects.filter(
        employee_id=employee, attendance_date=attendance_date
    )
    if not attendance.exists():
        attendance = Attendance()
        attendance.employee_id = employee
        attendance.shift_id = shift
        attendance.work_type_id = attendance.employee_id.employee_work_info.work_type_id
        attendance.attendance_date = attendance_date
        attendance.attendance_day = day
        attendance.attendance_clock_in = now
        attendance.attendance_clock_in_date = date_today
        attendance.minimum_hour = minimum_hour
        # Resolved and snapshotted once, here, at the first clock-in of the
        # day -- never re-resolved afterward, so a mode switch that takes
        # effect mid-session doesn't change this day's already-decided
        # behavior. See Attendance.attendance_rule_set's field comment.
        attendance.attendance_rule_set = AttendanceRuleSet.resolve_for_employee(
            employee
        )
        # Captured once, here, alongside the FK above -- this row's own
        # raw values plus the Company Default's, for
        # resolve_effective_value() to read from later without ever
        # touching the live rows again. See the field's comment.
        attendance.attendance_rule_set_snapshot = AttendanceRuleSet.capture_snapshot(
            attendance.attendance_rule_set
        )
        attendance.geo_fence_violation = geo_fence_violation
        attendance.geo_fence_unverified = geo_fence_unverified
        attendance.save()
        # Late-come detection doesn't apply under Flexible mode -- there's
        # no shift to be late against. Deferred to a background task
        # (Part 3 of the Attendance performance plan) -- the punch itself
        # is already durable above; this is purely a consequence of it,
        # safe to run a moment later and harmless to redo if it fails.
        if not attendance.is_flexible_mode():
            _enqueue_late_come_early_out(attendance)
    else:
        attendance = attendance[0]
        attendance.attendance_clock_out = None
        attendance.attendance_clock_out_date = None
        # OR'd, never overwritten with False -- a flag raised earlier
        # today (e.g. this morning's clock-in) must survive a later,
        # clean re-clock-in.
        attendance.geo_fence_violation = (
            attendance.geo_fence_violation or geo_fence_violation
        )
        attendance.geo_fence_unverified = (
            attendance.geo_fence_unverified or geo_fence_unverified
        )
        attendance.save()
        # delete if the attendance marked the early out or a Flexible-
        # mode shortfall -- both are based on an earlier, partial-day
        # clock-out; re-clocking in means more hours are still coming,
        # so a stale marker here would misrepresent the full day once
        # it's actually finished. The next real clock-out re-evaluates
        # correctly with the fuller picture either way.
        attendance.late_come_early_out.filter(
            type__in=["early_out", "flexible_shortfall"]
        ).delete()
    return attendance


@login_required
@hx_request_required
def clock_in(request):
    """
    This method is used to mark the attendance once per a day and multiple attendance activities.
    """
    # check wether check in/check out feature is enabled
    selected_company = request.session.get("selected_company")
    if selected_company == "all":
        company = None
        attendance_general_settings = get_cached_attendance_general_settings(None)
    else:
        company = Company.objects.filter(id=selected_company).first()
        attendance_general_settings = get_cached_attendance_general_settings(company)
    # request.__dict__.get("datetime")' used to check if the request is from a biometric device
    if (
        attendance_general_settings
        and attendance_general_settings.enable_check_in
        or request.__dict__.get("datetime")
    ):
        employee, work_info = employee_exists(request)
        datetime_now = timezone.localtime()
        if request.__dict__.get("datetime"):
            datetime_now = request.datetime
        if employee and work_info is not None:
            shift = work_info.shift_id
            date_today = date.today()
            if request.__dict__.get("date"):
                date_today = request.date
            attendance_date = date_today
            day = date_today.strftime("%A").lower()
            day = EmployeeShiftDay.objects.get(day=day)
            now = datetime.now().strftime("%H:%M")
            if request.__dict__.get("time"):
                now = request.time.strftime("%H:%M")
            now_sec = strtime_seconds(now)
            mid_day_sec = strtime_seconds("12:00")
            minimum_hour, start_time_sec, end_time_sec = shift_schedule_today(
                day=day, shift=shift
            )
            if start_time_sec > end_time_sec:
                # night shift
                # ------------------
                # Night shift in Horilla consider a 24 hours from noon to next day noon,
                # the shift day taken today if the attendance clocked in after 12 O clock.

                if mid_day_sec > now_sec:
                    # Here you need to create attendance for yesterday

                    date_yesterday = date_today - timedelta(days=1)
                    day_yesterday = date_yesterday.strftime("%A").lower()
                    day_yesterday = EmployeeShiftDay.objects.get(day=day_yesterday)
                    minimum_hour, start_time_sec, end_time_sec = shift_schedule_today(
                        day=day_yesterday, shift=shift
                    )
                    attendance_date = date_yesterday
                    day = day_yesterday
            attendance = clock_in_attendance_and_activity(
                employee=employee,
                date_today=date_today,
                attendance_date=attendance_date,
                day=day,
                now=now,
                shift=shift,
                minimum_hour=minimum_hour,
                start_time=start_time_sec,
                end_time=end_time_sec,
                in_datetime=datetime_now,
            )
            # Refresh employee from DB so template re-evaluates is_clocked_in correctly
            employee.refresh_from_db()
            return render(
                request, "attendance/components/in_out_component.html", {"run": 1}
            )
        messages.error(
            request,
            _(
                "Check-In Unavailable: Your employee profile or work information is incomplete."
            ),
        )
        return HorillaRedirect(request)
    else:
        messages.error(
            request,
            _(
                "The attendance check-in/check-out feature has not been enabled for your company."
            ),
        )
        return HorillaRedirect(request)


def clock_out_attendance_and_activity(
    employee,
    date_today,
    now,
    out_datetime=None,
    latitude=None,
    longitude=None,
    geo_fence_violation=False,
    geo_fence_unverified=False,
):
    """
    Clock out the attendance and activity
    args:
        employee    : employee instance
        date_today  : today date
        now         : now
        latitude, longitude        : Geo-tag coordinates from a mobile
                                      punch (None for a web punch)
        geo_fence_violation,
        geo_fence_unverified        : this punch's Geo-mark check outcome
                                       (see clock_in_attendance_and_activity)
    """

    attendance_activities = AttendanceActivity.objects.filter(
        employee_id=employee,
    ).order_by("attendance_date", "id")
    attendance_activity = None  # Initialize attendance_activity

    if attendance_activities.filter(clock_out__isnull=True).exists():
        attendance_activity = attendance_activities.filter(
            clock_out__isnull=True
        ).last()
        attendance_activity.clock_out = out_datetime
        attendance_activity.clock_out_date = date_today
        attendance_activity.out_datetime = out_datetime
        attendance_activity.clock_out_latitude = latitude
        attendance_activity.clock_out_longitude = longitude
        attendance_activity.save()

        attendance_activities = attendance_activities.filter(
            attendance_date=attendance_activity.attendance_date
        )
        # Here calculate the total durations between the attendance activities

        duration = 0
        for activity in attendance_activities:
            in_datetime, out_datetime = activity_datetime(activity)
            difference = out_datetime - in_datetime
            days_second = difference.days * 24 * 3600
            seconds = difference.seconds
            total_seconds = days_second + seconds
            duration = duration + total_seconds
        duration = format_time(duration)
        # update clock out of attendance
        attendance = Attendance.objects.filter(employee_id=employee).order_by(
            "-attendance_date", "-id"
        )[0]
        attendance.attendance_clock_out = now + ":00"
        attendance.attendance_clock_out_date = date_today
        attendance.attendance_worked_hour = duration
        # Compute overtime/auto-approve now, ahead of the save() below
        # that would normally do this -- attendance_validate() needs the
        # freshly-computed overtime_second/attendance_overtime_approve
        # for THIS clock-out to decide validation, and save() hasn't run
        # yet at this point. Both methods are pure functions of fields
        # already set on this instance, so save() calling them again
        # moments later recomputes the identical result -- harmless.
        attendance.update_attendance_overtime()
        attendance.handle_overtime_conditions()

        # Validate the attendance as per the condition
        attendance.attendance_validated = attendance_validate(attendance)
        # OR'd, never overwritten with False -- see the matching comment
        # in clock_in_attendance_and_activity().
        attendance.geo_fence_violation = (
            attendance.geo_fence_violation or geo_fence_violation
        )
        attendance.geo_fence_unverified = (
            attendance.geo_fence_unverified or geo_fence_unverified
        )
        attendance.save()

        return attendance

    logger.error("No attendance clock in activity found that needs clocking out.")
    return


def early_out_create(attendance):
    """
    Used to create early out report
    args:
        attendance : attendance obj
    """
    if AttendanceLateComeEarlyOut.objects.filter(
        type="early_out", attendance_id=attendance
    ).exists():
        late_come_obj = AttendanceLateComeEarlyOut.objects.filter(
            type="early_out", attendance_id=attendance
        ).first()
    else:
        late_come_obj = AttendanceLateComeEarlyOut()
    late_come_obj.type = "early_out"
    late_come_obj.attendance_id = attendance
    late_come_obj.employee_id = attendance.employee_id
    late_come_obj.save()
    return late_come_obj


def early_out(attendance, start_time, end_time, shift):
    """
    This method is used to mark the early check-out attendance before the shift ends
    args:
        attendance : attendance obj
        start_time : attendance day shift start time
        start_end : attendance day shift end time
    """
    if not shift:
        return
    if not enable_late_come_early_out_tracking(None).get("tracking"):
        return

    clock_out_time = attendance.attendance_clock_out
    if isinstance(clock_out_time, str):
        clock_out_time = datetime.strptime(clock_out_time, "%H:%M:%S")

    now_sec = strtime_seconds(clock_out_time.strftime("%H:%M"))
    mid_day_sec = strtime_seconds("12:00")
    # Checking gracetime allowance before creating early out
    if shift and shift.grace_time_id:
        if (
            shift.grace_time_id.is_active == True
            and shift.grace_time_id.allowed_clock_out == True
        ):
            now_sec += shift.grace_time_id.allowed_time_in_secs
    elif GraceTime.objects.filter(is_default=True, is_active=True).exists():
        grace_time = GraceTime.objects.filter(
            is_default=True,
            is_active=True,
        ).first()
        # Setting allowance for the check out time if grace allocate for clock out event
        if grace_time.allowed_clock_out:
            now_sec += grace_time.allowed_time_in_secs
    else:
        pass
    if start_time > end_time:
        # Early out condition for night shift
        if now_sec < mid_day_sec:
            if now_sec < end_time:
                # Early out condition for general shift
                early_out_create(attendance)
        else:
            early_out_create(attendance)
        return
    if end_time > now_sec:
        early_out_create(attendance)
    return


def flexible_shortfall_create(attendance):
    """
    Used to create a Flexible-mode hours-shortfall irregularity report --
    the same AttendanceLateComeEarlyOut mechanism used for late-come/
    early-out, one more category rather than a new record type.
    args:
        attendance : attendance obj
    """
    if AttendanceLateComeEarlyOut.objects.filter(
        type="flexible_shortfall", attendance_id=attendance
    ).exists():
        record = AttendanceLateComeEarlyOut.objects.filter(
            type="flexible_shortfall", attendance_id=attendance
        ).first()
    else:
        record = AttendanceLateComeEarlyOut()
    record.type = "flexible_shortfall"
    record.attendance_id = attendance
    record.employee_id = attendance.employee_id
    record.save()
    return record


def flexible_shortfall(attendance):
    """
    Irregularities' Flexible-mode half: records a shortfall if this
    day's worked hours fell short of the resolved total_work_hours_
    reference. Purely informational, same as late-come/early-out --
    Irregularities is a visibility-only screen for MVP (no approval
    action), a different lens from Validation: a record can be fully
    validated and still show up here, since this is about timing
    patterns, not whether the record itself is trustworthy. Opt-in via
    irregularities_enabled, same convention as every other new setting
    this feature set introduced.
    args:
        attendance : attendance obj
    """
    resolve = AttendanceRuleSet.resolve_effective_value
    snapshot = attendance.attendance_rule_set_snapshot
    if not resolve(snapshot, "irregularities_enabled"):
        return
    reference_hours = resolve(snapshot, "total_work_hours_reference")
    if reference_hours in (None, ""):
        return
    reference_seconds = int(Decimal(reference_hours) * 3600)
    worked_seconds = strtime_seconds(attendance.attendance_worked_hour)
    if worked_seconds < reference_seconds:
        flexible_shortfall_create(attendance)


@login_required
@hx_request_required
def clock_out(request):
    """
    This method is used to set the out date and time for attendance and attendance activity
    """
    # check wether check in/check out feature is enabled
    selected_company = request.session.get("selected_company")
    if selected_company == "all":
        company = None
        attendance_general_settings = get_cached_attendance_general_settings(None)
    else:
        company = Company.objects.filter(id=selected_company).first()
        attendance_general_settings = get_cached_attendance_general_settings(company)
    if (
        attendance_general_settings
        and attendance_general_settings.enable_check_in
        or request.__dict__.get("datetime")
    ):
        datetime_now = timezone.localtime()
        if request.__dict__.get("datetime"):
            datetime_now = request.datetime
        employee, work_info = employee_exists(request)
        date_today = date.today()
        if request.__dict__.get("date"):
            date_today = request.date
        day = date_today.strftime("%A").lower()
        day = EmployeeShiftDay.objects.get(day=day)
        attendance = (
            Attendance.objects.filter(employee_id=employee)
            .order_by("id", "attendance_date")
            .last()
        )
        if attendance is not None:
            if not attendance.attendance_day:
                day_name = attendance.attendance_date.strftime("%A").lower()
                attendance.attendance_day = EmployeeShiftDay.objects.get(day=day_name)
                attendance.save(update_fields=["attendance_day"])
            day = attendance.attendance_day
        now = datetime.now().strftime("%H:%M")
        if request.__dict__.get("time"):
            now = request.time.strftime("%H:%M")
        # start_time_sec/end_time_sec/minimum_hour used to be resolved
        # here for the early_out()/flexible_shortfall() calls below --
        # both are now deferred to a background task (Part 3 of the
        # Attendance performance plan) that re-derives them itself, so
        # this hot-path lookup is no longer needed at all.
        attendance = clock_out_attendance_and_activity(
            employee=employee,
            date_today=date_today,
            now=now,
            out_datetime=datetime_now,
            latitude=request.__dict__.get("latitude"),
            longitude=request.__dict__.get("longitude"),
            geo_fence_violation=request.__dict__.get("geo_fence_violation", False),
            geo_fence_unverified=request.__dict__.get("geo_fence_unverified", False),
        )
        if attendance:
            # Early-out detection doesn't apply under Flexible mode --
            # there's no shift to be early against; the Irregularities
            # counterpart there is a worked-hours shortfall check
            # instead (see flexible_shortfall()). Uses the mode
            # snapshotted at this day's first clock-in, not a fresh
            # resolution. The gating logic below (whether to check at
            # all) stays synchronous -- it's pure computation, no DB
            # writes; only the actual flagging call is deferred (Part 3
            # of the Attendance performance plan).
            if attendance.is_flexible_mode():
                _enqueue_late_come_early_out(attendance)
            else:
                early_out_instance = attendance.late_come_early_out.filter(
                    type="early_out"
                )
                is_night_shift = attendance.is_night_shift()
                next_date = attendance.attendance_date + timedelta(days=1)
                if not early_out_instance.exists():
                    if is_night_shift:
                        now_sec = strtime_seconds(now)
                        mid_sec = strtime_seconds("12:00")

                        if (attendance.attendance_date == date_today) or (
                            # check is next day mid
                            mid_sec >= now_sec
                            and date_today == next_date
                        ):
                            _enqueue_late_come_early_out(attendance)
                    elif attendance.attendance_date == date_today:
                        _enqueue_late_come_early_out(attendance)

        # Refresh employee from DB so template re-evaluates is_clocked_in correctly
        employee.refresh_from_db()
        return render(
            request, "attendance/components/in_out_component.html", {"run": 1}
        )

    else:
        messages.error(
            request,
            _(
                "The attendance check-in/check-out feature has not been enabled for your company."
            ),
        )
        return HorillaRedirect(request)
