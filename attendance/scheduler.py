import datetime
import sys
from datetime import timedelta

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from django.conf import settings
from django.utils import timezone

from base.backends import logger


def _auto_punch_out_company(employee):
    """
    The company an open attendance's employee belongs to, or None. Same
    lookup shape as TieredConfigResolutionMixin.resolve_for_employee() in
    attendance/config_tiers.py.
    """
    work_info = getattr(employee, "employee_work_info", None)
    return getattr(work_info, "company_id", None)


def _is_auto_punch_out_enabled_for(company):
    """
    The company-wide master switch (AttendanceGeneralSetting.
    auto_punch_out_enabled), falling back to the global (company_id=None)
    row -- same fallback shape as the per-request lookup in
    attendance/views/clock_in_out.py's clock_in()/clock_out(). Off by
    default is never possible (field defaults True), so a missing row
    (shouldn't happen -- attendance.signals.create_attendance_setting
    creates one per company) fails open rather than silently disabling
    Auto Punch-out everywhere.
    """
    from attendance.models import AttendanceGeneralSetting

    setting = AttendanceGeneralSetting.objects.filter(company_id=company).first()
    if setting is None:
        setting = AttendanceGeneralSetting.objects.filter(company_id=None).first()
    return setting is None or setting.auto_punch_out_enabled


def _close_attendance(attendance, at_datetime, at_time):
    from attendance.methods.utils import Request
    from attendance.views.clock_in_out import clock_out

    try:
        clock_out(
            Request(
                user=attendance.employee_id.employee_user_id,
                date=at_datetime.date(),
                time=at_time,
                datetime=at_datetime,
            )
        )
    except Exception as e:
        logger.error(f"auto_punch_out error: {e}")


def _auto_punch_out_shift_based():
    """
    Existing behavior: an employee on a shift whose EmployeeShiftSchedule
    has is_auto_punch_out_enabled=True gets auto-closed at that
    schedule's own auto_punch_out_time. Gated by the new company-wide
    master switch -- everything else here is unchanged.
    """
    from attendance.models import Attendance, AttendanceActivity
    from base.models import EmployeeShiftSchedule

    automatic_check_out_shifts = EmployeeShiftSchedule.objects.filter(
        is_auto_punch_out_enabled=True
    )

    for shift_schedule in automatic_check_out_shifts:
        activities = AttendanceActivity.objects.filter(
            shift_day=shift_schedule.day,
            clock_out_date=None,
            clock_out=None,
        ).order_by("-created_at")

        for activity in activities:
            attendance = Attendance.objects.filter(
                employee_id=activity.employee_id,
                attendance_clock_out=None,
                attendance_clock_out_date=None,
                shift_id=shift_schedule.shift_id,
                attendance_day=shift_schedule.day,
                attendance_date=activity.attendance_date,
            ).first()

            if not attendance:
                continue

            if not _is_auto_punch_out_enabled_for(
                _auto_punch_out_company(attendance.employee_id)
            ):
                continue

            date = activity.attendance_date
            if (
                shift_schedule.is_night_shift
                and shift_schedule.start_time
                and shift_schedule.end_time
                and shift_schedule.start_time > shift_schedule.end_time
            ):
                date += timedelta(days=1)

            combined_datetime = timezone.make_aware(
                datetime.datetime.combine(date, shift_schedule.auto_punch_out_time)
            )

            if combined_datetime < timezone.now():
                _close_attendance(
                    attendance, combined_datetime, shift_schedule.auto_punch_out_time
                )


def _auto_punch_out_flexible_and_no_shift():
    """
    The two cases the shift-based sweep above structurally can't cover,
    since both key off a real EmployeeShiftSchedule that doesn't exist
    for these employees:
      - Flexible mode: cutoff is AttendanceRuleSet.auto_punch_out_cutoff_time
        (resolved through get_effective_values(), so an Employee-Type
        override left blank still inherits the Company Default's cutoff).
      - No shift at all: a flat cutoff from AttendanceGeneralSetting.
        no_shift_auto_punch_out_time.
    Deliberately skips any attendance with a real shift assigned (even one
    without is_auto_punch_out_enabled) -- that's the shift-based sweep's
    job to leave alone or not, this loop never overrides that choice.
    """
    from attendance.models import Attendance, AttendanceGeneralSetting

    open_attendances = Attendance.objects.filter(
        attendance_clock_out=None, attendance_clock_out_date=None,
    ).select_related("attendance_rule_set")

    for attendance in open_attendances:
        company = _auto_punch_out_company(attendance.employee_id)
        if not _is_auto_punch_out_enabled_for(company):
            continue

        if attendance.is_flexible_mode():
            rule_set = attendance.attendance_rule_set
            cutoff_time = (
                rule_set.get_effective_values().get("auto_punch_out_cutoff_time")
                if rule_set is not None
                else None
            )
            if cutoff_time is None:
                continue
        elif attendance.shift_id is None:
            setting = AttendanceGeneralSetting.objects.filter(
                company_id=company
            ).first() or AttendanceGeneralSetting.objects.filter(
                company_id=None
            ).first()
            cutoff_time = (
                setting.no_shift_auto_punch_out_time
                if setting is not None
                else datetime.time(23, 59)
            )
        else:
            # Has a real shift and isn't Flexible -- the shift-based
            # sweep owns this row.
            continue

        combined_datetime = timezone.make_aware(
            datetime.datetime.combine(attendance.attendance_date, cutoff_time)
        )
        if combined_datetime < timezone.now():
            _close_attendance(attendance, combined_datetime, cutoff_time)


def auto_punch_out():
    _auto_punch_out_shift_based()
    _auto_punch_out_flexible_and_no_shift()


def apply_pending_config_changes():
    """
    Finds every PendingConfigChange whose effective_date has arrived and
    applies it. `__lte` (not exact-match) makes this self-healing if a
    scheduler run was missed -- a change just applies on the next run
    instead of being skipped. `apply()` itself is a no-op on anything not
    still `pending`, so re-running this job is always safe.
    """
    from datetime import date

    from attendance.models import PendingConfigChange

    due = PendingConfigChange.objects.filter(
        status=PendingConfigChange.STATUS_PENDING,
        effective_date__lte=date.today(),
    )
    for change in due:
        try:
            change.apply()
        except Exception as e:
            logger.error(f"apply_pending_config_changes error for change {change.pk}: {e}")


def create_work_record():
    from attendance.models import WorkRecords
    from employee.models import Employee

    date = datetime.date.today()
    work_records = WorkRecords.objects.filter(date=date).values_list(
        "employee_id", flat=True
    )
    employees = Employee.objects.exclude(id__in=work_records)
    records_to_create = []

    for employee in employees:
        try:
            shift_schedule = employee.get_shift_schedule()
            if shift_schedule is None:
                continue

            shift = employee.get_shift()
            record = WorkRecords(
                employee_id=employee,
                date=date,
                work_record_type="DFT",
                shift_id=shift,
                message="",
            )
            records_to_create.append(record)
        except Exception as e:
            logger.error(f"Error preparing work record for {employee}: {e}")

    if records_to_create:
        try:
            WorkRecords.objects.bulk_create(records_to_create, ignore_conflicts=True)
        except Exception as e:
            logger.error(f"Failed to bulk create work records: {e}")


if not any(
    cmd in sys.argv
    for cmd in ["makemigrations", "migrate", "compilemessages", "flush", "shell"]
):
    """
    Initializes and starts background tasks using APScheduler when the server is running.
    """
    scheduler = BackgroundScheduler(timezone=pytz.timezone(settings.TIME_ZONE))

    scheduler.add_job(
        create_work_record, "interval", minutes=30, misfire_grace_time=3600 * 3
    )
    scheduler.add_job(
        create_work_record,
        "cron",
        hour=0,
        minute=30,
        misfire_grace_time=3600 * 9,
        id="create_daily_work_record",
        replace_existing=True,
    )
    scheduler.add_job(
        auto_punch_out,
        "interval",
        minutes=5,
        misfire_grace_time=600,
        id="auto_punch_out",
        replace_existing=True,
    )
    scheduler.add_job(
        apply_pending_config_changes,
        "cron",
        hour=0,
        minute=45,
        misfire_grace_time=3600 * 9,
        id="apply_pending_config_changes",
        replace_existing=True,
    )

    scheduler.start()
