"""
models.py

This module is used to register models for recruitment app

"""

import contextlib
import datetime as dt
import json
from datetime import date, datetime, timedelta
from decimal import Decimal

import pandas as pd
from django.apps import apps
from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models, transaction
from django.db.models import F, Q
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from attendance.methods.utils import (
    MONTH_MAPPING,
    attendance_date_validate,
    format_time,
    get_diff_dict,
    strtime_seconds,
    validate_hh_mm_ss_format,
    validate_time_format,
    validate_time_in_minutes,
)
from base.config_tiers import (
    TIER_CHOICES,
    TIER_COMPANY,
    TIER_DEPARTMENT,
    TIER_EMPLOYEE_TYPE,
    TieredConfigResolutionMixin,
    config_override_applied,
    config_override_cancelled,
    config_override_requested,
    next_month_first,
)
from base.caching import get_cached_is_company_leave, get_cached_is_holiday
from base.horilla_company_manager import HorillaCompanyManager
from base.models import (
    COLLAR_CATEGORY_CHOICES,
    Company,
    Department,
    EmployeeShift,
    EmployeeShiftDay,
    WorkType,
)
from employee.models import Employee

# Create your models here.
from horilla.methods import get_horilla_model_class
from horilla.models import HorillaModel, upload_path
from horilla_audit.models import HorillaAuditInfo, HorillaAuditLog
from horilla_auth.models import HorillaUser
from horilla_views.cbv_methods import render_template

# to skip the migration issue with the old migrations
_validate_time_in_minutes = validate_time_in_minutes


# Create your models here.


class AttendanceActivity(HorillaModel):
    """
    AttendanceActivity model
    """

    employee_id = models.ForeignKey(
        Employee,
        on_delete=models.PROTECT,
        related_name="employee_attendance_activities",
        verbose_name=_("Employee"),
    )
    attendance_date = models.DateField(
        null=True,
        validators=[attendance_date_validate],
        verbose_name=_("Attendance Date"),
    )
    shift_day = models.ForeignKey(
        EmployeeShiftDay,
        null=True,
        on_delete=models.DO_NOTHING,
        verbose_name=_("Shift Day"),
    )
    in_datetime = models.DateTimeField(null=True)
    clock_in_date = models.DateField(null=True, verbose_name=_("In Date"))
    clock_in = models.TimeField(verbose_name=_("Check In"))
    clock_out_date = models.DateField(null=True, verbose_name=_("Out Date"))
    out_datetime = models.DateTimeField(null=True)
    clock_out = models.TimeField(null=True, verbose_name=_("Check Out"))
    # Geo-tag: a passive GPS stamp recorded with every mobile punch (never
    # set for a web punch). Separate pairs for in vs out -- one clock-in
    # and one clock-out can legitimately happen at different locations,
    # and this one row already represents both halves of a session.
    clock_in_latitude = models.FloatField(
        null=True, blank=True, verbose_name=_("Clock-In Latitude")
    )
    clock_in_longitude = models.FloatField(
        null=True, blank=True, verbose_name=_("Clock-In Longitude")
    )
    clock_out_latitude = models.FloatField(
        null=True, blank=True, verbose_name=_("Clock-Out Latitude")
    )
    clock_out_longitude = models.FloatField(
        null=True, blank=True, verbose_name=_("Clock-Out Longitude")
    )
    objects = HorillaCompanyManager(
        related_company_field="employee_id__employee_work_info__company_id"
    )
    history = HorillaAuditLog(
        related_name="history_set",
        bases=[
            HorillaAuditInfo,
        ],
    )

    class Meta:
        """
        Meta class to add some additional options
        """

        ordering = ["-attendance_date", "employee_id__employee_first_name", "clock_in"]

    def get_status(self):
        """
        Display status
        """

        DAY = [
            ("monday", _("Monday")),
            ("tuesday", _("Tuesday")),
            ("wednesday", _("Wednesday")),
            ("thursday", _("Thursday")),
            ("friday", _("Friday")),
            ("saturday", _("Saturday")),
            ("sunday", _("Sunday")),
        ]
        return dict(DAY).get(self.shift_day.day)

    def get_delete_attendance(self):
        """
        for delete button
        """

        return render_template(
            path="cbv/attendance_activity/delete_action.html",
            context={"instance": self},
        )

    def attendance_detail_subtitle(self):
        """
        Return subtitle containing both department and job position information.
        """
        return f"{self.employee_id.get_department()} / {self.employee_id.get_job_position()}"

    def attendance_detail_view(self):
        """
        for detail view of page
        """
        url = reverse("attendance-activity-single-view", kwargs={"pk": self.pk})
        return url

    def diff_cell(self):
        if self.clock_out == None:
            return 'style="background-color: #FFE4B3"'

    def detail_view_delete_attendance(self):
        """
        for delete button
        """

        return render_template(
            path="cbv/attendance_activity/detail_delete_action.html",
            context={"instance": self},
        )

    def duration_format_time(self, seconds):
        """
        This method is used to format seconds to H:M:S and return it
        args:
            seconds : seconds
        """
        hour = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        seconds = int((seconds % 3600) % 60)
        return f"{hour:02d}:{minutes:02d}:{seconds:02d}"

    def duration(self):
        """
        Duration calc b/w in-out method
        """

        if not self.clock_out or not self.clock_out_date:
            self.clock_out_date = datetime.today().date()
            self.clock_out = datetime.now().time()

        clock_in_datetime = datetime.combine(self.clock_in_date, self.clock_in)
        clock_out_datetime = datetime.combine(self.clock_out_date, self.clock_out)

        time_difference = clock_out_datetime - clock_in_datetime

        return time_difference.total_seconds()

    def duration_format(self):
        """
        Function to return the duration time in hh:mm:ss
        """
        total_seconds = self.duration()
        formatted_duration = self.duration_format_time(total_seconds)

        return formatted_duration

    def __str__(self):
        return f"{self.employee_id} - {self.attendance_date} - {self.clock_in} - {self.clock_out}"


class BatchAttendance(HorillaModel):
    """
    Batch attendance model
    """

    title = models.CharField(max_length=150, verbose_name=_("Title"))

    def __str__(self):
        return f"{self.title}-{self.id}"


class Attendance(HorillaModel):
    """
    Attendance model
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    status = [
        ("create_request", _("Create Request")),
        ("update_request", _("Update Request")),
        ("created_request", _("Created Request")),
    ]

    employee_id = models.ForeignKey(
        Employee,
        on_delete=models.PROTECT,
        null=True,
        related_name="employee_attendances",
        verbose_name=_("Employee"),
    )
    attendance_date = models.DateField(
        null=False,
        validators=[attendance_date_validate],
        verbose_name=_("Attendance date"),
    )
    shift_id = models.ForeignKey(
        EmployeeShift, on_delete=models.SET_NULL, null=True, verbose_name=_("Shift")
    )
    work_type_id = models.ForeignKey(
        WorkType,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,  # 796
        verbose_name=_("Work Type"),
    )
    attendance_day = models.ForeignKey(
        EmployeeShiftDay,
        on_delete=models.DO_NOTHING,
        null=True,
        verbose_name=_("Attendance day"),
    )
    # Snapshot of which AttendanceRuleSet row governed this day, resolved
    # once at the first clock-in and never re-resolved afterward -- so a
    # mode switch that takes effect mid-session (e.g. a night shift
    # spanning the 1st of the month) never changes an already-open day's
    # behavior. String reference: AttendanceRuleSet is defined later in
    # this same module. SET_NULL (not PROTECT) so deleting an old
    # Department/Employee-Type (which cascades to its AttendanceRuleSet
    # rows) is never blocked by historical attendance records.
    attendance_rule_set = models.ForeignKey(
        "attendance.AttendanceRuleSet",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        editable=False,
        related_name="attendances",
        verbose_name=_("Attendance Rule Set (snapshot)"),
    )
    # The FK above freezes WHICH row governs this day, but that row can
    # still be edited later (PendingConfigChange.apply() mutates it in
    # place now -- its own edit history lives in AttendanceRuleSet.history
    # instead, see that field's comment) -- so the FK alone doesn't
    # protect an open session from seeing changed values mid-session.
    # What actually does: this field, captured once at clock-in via
    # AttendanceRuleSet.capture_snapshot() -- this row's own raw field
    # values, plus the Company Default's raw values for anything left
    # blank to inherit. Every later read (attendance_validate(), auto
    # punch-out's Flexible cutoff, etc.) resolves purely from this frozen
    # data via AttendanceRuleSet.resolve_effective_value(), never by
    # calling get_effective_values() live on attendance_rule_set again.
    attendance_rule_set_snapshot = models.JSONField(
        null=True,
        blank=True,
        editable=False,
        encoder=DjangoJSONEncoder,
        verbose_name=_("Attendance Rule Set Snapshot"),
    )
    attendance_clock_in_date = models.DateField(
        null=True, verbose_name=_("Check-In Date")
    )
    attendance_clock_in = models.TimeField(
        null=True, verbose_name=_("Check-In"), help_text=_("First Check-In Time")
    )
    attendance_clock_out_date = models.DateField(
        null=True, verbose_name=_("Check-Out Date")
    )
    attendance_clock_out = models.TimeField(
        null=True, verbose_name=_("Check-Out"), help_text=_("Last Check-Out Time")
    )
    attendance_worked_hour = models.CharField(
        null=True,
        default="00:00",
        max_length=10,
        validators=[validate_time_format],
        verbose_name=_("Worked Hours"),
    )
    minimum_hour = models.CharField(
        max_length=10,
        default="00:00",
        validators=[validate_time_format],
        verbose_name=_("Minimum hour"),
    )
    batch_attendance_id = models.ForeignKey(
        BatchAttendance,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        verbose_name=_("Batch Attendance"),
    )
    attendance_overtime = models.CharField(
        default="00:00",
        validators=[validate_time_format],
        max_length=10,
        verbose_name=_("Overtime"),
    )
    attendance_overtime_approve = models.BooleanField(
        default=False, verbose_name=_("Overtime Approve")
    )
    # Distinguishes "not yet manually reviewed" from "actively denied" --
    # attendance_overtime_approve alone can't: it's False in both cases.
    # Needed for My Krew Attendance's Overtime queue (Need Your Action)
    # to know when a day has already been decided and should drop out of
    # the pending list, same PENDING/APPROVED/DENIED vocabulary as
    # RegularizationRequest.overtime_decision (a different dispute-
    # specific decision, not shared with this one -- that one resolves
    # an employee-raised dispute; this one is the manager-initiated
    # review this day's overtime never got in the first place).
    OVERTIME_DECISION_PENDING = "PENDING"
    OVERTIME_DECISION_APPROVED = "APPROVED"
    OVERTIME_DECISION_DENIED = "DENIED"
    OVERTIME_DECISION_CHOICES = (
        (OVERTIME_DECISION_PENDING, _("Pending")),
        (OVERTIME_DECISION_APPROVED, _("Approved")),
        (OVERTIME_DECISION_DENIED, _("Denied")),
    )
    overtime_decision = models.CharField(
        max_length=10,
        choices=OVERTIME_DECISION_CHOICES,
        default=OVERTIME_DECISION_PENDING,
        verbose_name=_("Overtime Decision"),
    )
    attendance_validated = models.BooleanField(
        default=False, verbose_name=_("Attendance Validate")
    )
    # Pure provenance tag, not a behavioral field -- lets the
    # Auto-Validated/Auto-Approved-OT logs and the Attendance Activity
    # Log distinguish "manually created by an admin/manager, Validation
    # and Overtime approval bypassed entirely" (Create Attendance) from
    # a genuine threshold-based auto-pass. Left blank for every other
    # creation path.
    CREATION_SOURCE_MANUAL = "MANUAL"
    CREATION_SOURCE_CHOICES = ((CREATION_SOURCE_MANUAL, _("Manually Created")),)
    creation_source = models.CharField(
        max_length=20,
        choices=CREATION_SOURCE_CHOICES,
        null=True,
        blank=True,
        verbose_name=_("Creation Source"),
    )
    # Purpose-specific Geo-location flags (not a generic catch-all field --
    # see the Validation feature's design decision: several independent
    # reasons for attention need to be representable at once). Set at
    # clock-in/clock-out via clock_in_out.py, OR'd onto whatever value is
    # already here rather than overwritten, so a flag raised earlier in
    # the day is never silently cleared by a later, clean punch.
    geo_fence_violation = models.BooleanField(
        default=False,
        verbose_name=_("Geo-fence Violation"),
        help_text=_(
            "A punch was confirmed outside the configured boundary and "
            "the boundary's enforcement mode is Flag, not Reject."
        ),
    )
    geo_fence_unverified = models.BooleanField(
        default=False,
        verbose_name=_("Geo-fence Unverified"),
        help_text=_(
            "A punch's location could not be checked against the "
            "configured boundary (not the same as a confirmed violation)."
        ),
    )
    at_work_second = models.IntegerField(null=True, blank=True)
    overtime_second = models.IntegerField(
        null=True, blank=True, verbose_name=_("Overtime In Second")
    )
    approved_overtime_second = models.IntegerField(default=0)
    is_validate_request = models.BooleanField(
        default=False, verbose_name=_("Is validate request")
    )
    is_bulk_request = models.BooleanField(default=False, editable=False)
    is_validate_request_approved = models.BooleanField(
        default=False, verbose_name=_("Is validate request approved")
    )
    request_description = models.TextField(
        null=True, verbose_name=_("Request Description")
    )
    request_type = models.CharField(
        max_length=18, null=True, choices=status, default="update_request"
    )
    is_holiday = models.BooleanField(default=False)
    requested_data = models.JSONField(null=True, editable=False)
    approved_by = models.ForeignKey(
        Employee,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        verbose_name=_("Approved By"),
        editable=False,
    )
    objects = HorillaCompanyManager(
        related_company_field="employee_id__employee_work_info__company_id"
    )
    history = HorillaAuditLog(
        related_name="history_set",
        bases=[
            HorillaAuditInfo,
        ],
    )

    def get_instance_id(self):
        return self.id

    def diff_cell(self):
        if self.request_type == "created_request":
            return 'style="background-color: #FFE4B3"'

    def status_col(self):
        """
        This method for get custome coloumn for rating.
        """

        return render_template(
            path="cbv/attendance_request/status.html",
            context={"instance": self},
        )

    def my_attendance_subtitle(self):
        """
        Detail view subtitle
        """

        return f"{self.employee_id.get_department()} / {self.employee_id.get_job_position()}"

    def my_attendance_detail(self):
        """
        detail view
        """

        url = reverse("my-attendance-detail", kwargs={"pk": self.pk})

        return url

    def attendance_detail_view(self):
        """
        detail view
        """

        url = reverse("attendances-tab-detail-view", kwargs={"pk": self.pk})

        return url

    class Meta:
        """
        Meta class to add some additional options
        """

        unique_together = ("employee_id", "attendance_date")
        permissions = [
            ("change_validateattendance", "Validate Attendance"),
            ("change_approveovertime", "Change Approve Overtime"),
            ("can_create_attendance", "Can manually create/override attendance"),
        ]
        ordering = [
            "-attendance_date",
            "employee_id__employee_first_name",
            "attendance_clock_in",
        ]
        verbose_name = _("Attendance")
        verbose_name_plural = _("Attendances")

    def check_min_ot(self):
        """
        Method to check the min ot for the attendance
        """

    def is_night_shift(self):
        """
        check is night shift or not
        """
        day = self.attendance_day
        if day is None:
            return False
        schedule = day.day_schedule.filter(shift_id=self.shift_id).first()
        if not schedule:
            return False
        return schedule.is_night_shift

    def get_shift_end_time(self):
        """
        This day's actual EmployeeShiftSchedule.end_time -- the baseline
        Shift-based overtime counts from, see update_attendance_overtime().
        Read live (like is_night_shift() above and shift_schedule_today()
        elsewhere), not snapshotted -- shift schedules aren't part of the
        tiered-config/PendingConfigChange system, they already change
        rarely and take effect immediately everywhere else too.
        """
        day = self.attendance_day
        if day is None:
            return None
        schedule = day.day_schedule.filter(shift_id=self.shift_id).first()
        if not schedule:
            return None
        return schedule.end_time

    def get_attendance_mode(self):
        """
        The Attendance Type mode (Shift-based/Flexible) snapshotted for
        this day at the first clock-in -- never re-resolved live, so a
        mode switch that takes effect mid-session never changes an
        already-open day's behavior. Resolved from
        attendance_rule_set_snapshot (see that field's comment), the
        single canonical source for every resolved rule-set value on
        this row -- never by reading attendance_rule_set.mode off the
        live row again. Falls back to the live FK's own field for rows
        created before that snapshot existed, then to Shift-based -- the
        PRD's default -- for a company that never configured a rule set
        at all.
        """
        mode = AttendanceRuleSet.resolve_effective_value(
            self.attendance_rule_set_snapshot, "mode"
        )
        if mode:
            return mode
        if self.attendance_rule_set_id:
            return self.attendance_rule_set.mode
        return AttendanceRuleSet.MODE_SHIFT_BASED

    def is_flexible_mode(self):
        """
        True if this day is governed by Flexible mode. Used to gate
        Shift-based-only behavior -- e.g. late-come/early-out detection
        doesn't apply under Flexible mode, since there's no shift to be
        late against.
        """
        return self.get_attendance_mode() == AttendanceRuleSet.MODE_FLEXIBLE

    def __str__(self) -> str:
        return f"{self.employee_id.employee_first_name} \
            {self.employee_id.employee_last_name} - {self.attendance_date}"

    def activities(self):
        """
        This method is used to return the activites and count of activites comes for an attendance
        """
        activities = AttendanceActivity.objects.filter(
            attendance_date=self.attendance_date, employee_id=self.employee_id
        )
        return {"query": activities, "count": activities.count()}

    def attendance_actions(self):
        """
        method for rendering actions(edit,delete)
        """

        return render_template(
            path="cbv/attendances/attendance_actions.html",
            context={"instance": self},
        )

    def comment_col(self):
        """
        This method for get custom coloumn for comment.
        """

        return render_template(
            path="cbv/attendance_request/comment.html",
            context={"instance": self},
        )

    def attendance_detail_activity_col(self):
        """
        this method is used to return attendance detail view activity custom col
        """

        return render_template(
            path="cbv/attendances/detail_view_activity_col.html",
            context={"instance": self},
        )

    def request_actions(self):
        """
        This method for get custom coloumn for comment.
        """

        return render_template(
            path="cbv/attendance_request/request_actions.html",
            context={"instance": self},
        )

    def request_options(self):
        """
        This method for get custom options for request.
        """
        return render_template(
            path="cbv/attendance_request/attendance_request_option.html",
            context={"instance": self},
        )

    def validate_detail_view(self):
        """
        detail view of validate tab
        """
        url = reverse("validate-detail-view", kwargs={"pk": self.pk})
        return url

    def individual_validate_detail_view(self):
        """
        detail view of validate tab
        """
        url = reverse("individual-validate-detail-view", kwargs={"pk": self.pk})
        return url

    def ot_detail_view(self):
        """
        detail view of OT tab
        """
        url = reverse("ot-detail-view", kwargs={"pk": self.pk})
        return url

    def validated_detail_view(self):
        """
        detail view of validated tab
        """
        url = reverse("validated-detail-view", kwargs={"pk": self.pk})
        return url

    def detail_view(self):
        """
        deteil view of requested attendances
        """
        url = reverse("validate-attendance-request", kwargs={"attendance_id": self.pk})
        return url

    def change_attendance(self):
        """
        Edit url
        """
        url = reverse("update-attendance-request", kwargs={"pk": self.pk})
        return url

    def ot_approve(self):
        """
        method for rendering approve OT
        """
        minot = strtime_seconds("00:30")
        condition = AttendanceValidationCondition.objects.first()
        if condition is not None:
            minot = strtime_seconds(condition.minimum_overtime_to_approve)

        return render_template(
            path="cbv/attendances/ot_confirmation.html",
            context={"instance": self, "minot": minot},
        )

    def validate_actions(self):
        """
        combined actions column for validate tab: validate + edit + delete
        """

        return render_template(
            path="cbv/attendances/validate_actions.html",
            context={"instance": self},
        )

    def ot_actions(self):
        """
        combined actions column for OT tab: approve OT + edit + delete
        """
        minot = strtime_seconds("00:30")
        condition = AttendanceValidationCondition.objects.first()
        if condition is not None:
            minot = strtime_seconds(condition.minimum_overtime_to_approve)

        return render_template(
            path="cbv/attendances/ot_actions.html",
            context={"instance": self, "minot": minot},
        )

    def validate_detail_actions(self):
        """
        detail view actions of validate tab
        """

        return render_template(
            path="cbv/attendances/validate_tab_action.html",
            context={"instance": self},
        )

    def ot_detail_actions(self):
        """
        detail view actions of OT tab
        """

        minot = strtime_seconds("00:30")
        condition = AttendanceValidationCondition.objects.first()
        if condition is not None:
            minot = strtime_seconds(condition.minimum_overtime_to_approve)

        return render_template(
            path="cbv/attendances/ot_tab_action.html",
            context={"instance": self, "minot": minot},
        )

    def validated_detail_actions(self):
        """
        detail view actions of validated tab
        """

        return render_template(
            path="cbv/attendances/validated_tab_action.html",
            context={"instance": self},
        )

    def validate_button(self):
        """
        detail view actions of validated tab
        """

        return render_template(
            path="cbv/attendances/validate_button.html",
            context={"instance": self},
        )

    def attendances_detail_subtitle(self):
        """
        Return subtitle containing both department and job position information.
        """
        return f"{self.employee_id.get_department()} / {self.employee_id.get_job_position()}"

    def activities(self):
        """
        This method is used to return the activites and count of activites comes for an attendance
        """
        activities = AttendanceActivity.objects.filter(
            attendance_date=self.attendance_date, employee_id=self.employee_id
        )
        return {"query": activities, "count": activities.count()}

    def requested_fields(self):
        """
        This method will returns the value difference fields
        """
        keys = []
        if self.requested_data is not None:
            data = json.loads(self.requested_data)
            diffs = get_diff_dict(self.serialize(), data)
            keys = diffs.keys()
        return keys

    def get_last_clock_out(self, null_activity=False):
        """
        This method is used to get the last attendance activity if exists
        """
        activities = AttendanceActivity.objects.filter(
            employee_id=self.employee_id,
            attendance_date=self.attendance_date,
            clock_out__isnull=null_activity,
        ).order_by("id")
        return activities.last()

    def get_at_work_from_activities(self):
        """
        This method is used to retun the at work calculated from the activities
        """
        activities = AttendanceActivity.objects.filter(
            attendance_date=self.attendance_date, employee_id=self.employee_id
        ).order_by("clock_in")
        at_work_seconds = 0
        now = datetime.now()
        for activity in activities:
            out_time = activity.clock_out
            if out_time is None:
                combined_out = datetime.combine(
                    now, dt.time(hour=now.hour, minute=now.minute, second=now.second)
                )
            else:
                combined_out = datetime.combine(activity.clock_out_date, out_time)
            in_time = activity.clock_in
            combined_in = datetime.combine(activity.clock_in_date, in_time)
            diffs = combined_out - combined_in
            at_work_seconds = at_work_seconds + diffs.total_seconds()
        return at_work_seconds

    def hours_pending(self):
        """
        This method will returns difference between minimum_hour and attendance_worked_hour
        """
        minimum_hours = strtime_seconds(self.minimum_hour)
        worked_hour = strtime_seconds(self.attendance_worked_hour)
        pending_seconds = minimum_hours - worked_hour
        if pending_seconds < 0:
            return "00:00"
        pending_hours = format_time(pending_seconds)
        return pending_hours

    def adjust_minimum_hour(self):
        """
        Set minimum_hour to 00:00 if the attendance date falls on a holiday or company leave.
        """
        if get_cached_is_holiday(
            self.attendance_date, self.employee_id
        ) or get_cached_is_company_leave(self.attendance_date):
            self.minimum_hour = "00:00"
            self.is_holiday = True
        else:
            self.is_holiday = False

    def update_attendance_overtime(self):
        """
        Calculate and update attendance overtime and worked seconds.

        Zero (and skipped entirely) if track_overtime doesn't resolve
        True for this day's rule set -- overtime is opt-in, not computed
        by default. Otherwise overtime starts at a mode-appropriate
        baseline plus one shared company-level ot_threshold_hours (see
        that field's comment for the worked examples):
          Shift-based: this day's actual shift end time
            (get_shift_end_time()) + ot_threshold_hours -- worked out
            entirely in real datetimes (not bare clock-time subtraction)
            so a late-ending shift crossing midnight still compares
            correctly.
          Flexible: total_work_hours_reference + ot_threshold_hours.
        Both fall back to the pre-existing worked-hour-vs-minimum_hour
        formula if the relevant settings aren't configured, or
        (Shift-based specifically) this day hasn't been clocked out yet
        -- there's no clock-out time yet to compare.
        """
        resolve = AttendanceRuleSet.resolve_effective_value
        snapshot = self.attendance_rule_set_snapshot
        track_overtime = resolve(snapshot, "track_overtime")

        self.at_work_second = strtime_seconds(self.attendance_worked_hour)

        if not track_overtime:
            self.attendance_overtime = "00:00"
            self.overtime_second = 0
            return

        threshold_hours = resolve(snapshot, "ot_threshold_hours")
        overtime_seconds = None

        if self.is_flexible_mode():
            baseline_hours = resolve(snapshot, "total_work_hours_reference")
            if baseline_hours not in (None, "") and threshold_hours not in (None, ""):
                effective_threshold_seconds = int(
                    (Decimal(baseline_hours) + Decimal(threshold_hours)) * 3600
                )
                overtime_seconds = max(
                    0, self.at_work_second - effective_threshold_seconds
                )
        else:
            shift_end_time = self.get_shift_end_time()
            if (
                shift_end_time
                and threshold_hours not in (None, "")
                and self.attendance_clock_out
                and self.attendance_clock_out_date
            ):
                clock_out_time = self.attendance_clock_out
                if isinstance(clock_out_time, str):
                    # Not yet coerced to a real time object -- this runs
                    # inside save(), and a caller (e.g.
                    # clock_out_attendance_and_activity()) may have just
                    # assigned a raw "HH:MM:SS" string moments earlier;
                    # Django's TimeField only converts it on the way to
                    # the database, not on plain attribute assignment.
                    clock_out_time = dt.time.fromisoformat(clock_out_time)
                clock_out_dt = datetime.combine(
                    self.attendance_clock_out_date, clock_out_time
                )
                shift_end_dt = datetime.combine(
                    self.attendance_clock_out_date, shift_end_time
                )
                ot_start_dt = shift_end_dt + timedelta(
                    hours=float(Decimal(threshold_hours))
                )
                overtime_seconds = max(
                    0, int((clock_out_dt - ot_start_dt).total_seconds())
                )

        if overtime_seconds is None:
            overtime_seconds = max(
                0, self.at_work_second - strtime_seconds(self.minimum_hour)
            )

        self.attendance_overtime = format_time(overtime_seconds)
        self.overtime_second = strtime_seconds(self.attendance_overtime)

    def handle_overtime_conditions(self):
        """
        Auto-approve overtime only while it stays within the configured
        buffer above the OT start point; anything beyond needs a
        manager's decision. This is the inverted comparison the PRD
        wants -- the previous AttendanceValidationCondition-based logic
        auto-approved once overtime reached *at least* a threshold,
        backwards from "auto-approve only while overtime stays small."
        Only ever sets the flag True here, never False -- a manual
        approve/reject decision made elsewhere must never be silently
        overwritten by this running again on an unrelated save.
        """
        if self.is_validate_request:
            self.is_validate_request_approved = self.attendance_validated = False

        resolve = AttendanceRuleSet.resolve_effective_value
        snapshot = self.attendance_rule_set_snapshot
        if not resolve(snapshot, "track_overtime"):
            return

        if self.is_flexible_mode():
            buffer_hours = resolve(snapshot, "flexible_ot_auto_approve_buffer_hours")
            buffer_seconds = (
                int(Decimal(buffer_hours) * 3600)
                if buffer_hours not in (None, "")
                else 0
            )
        else:
            buffer_minutes = resolve(snapshot, "shift_ot_auto_approve_buffer_minutes")
            buffer_seconds = (
                int(buffer_minutes) * 60 if buffer_minutes not in (None, "") else 0
            )

        # Strictly positive, not just "within buffer" -- this runs on
        # every save, including the very first one at clock-in, when
        # there's no clock-out yet and overtime is trivially 0. Since
        # this method deliberately never resets the flag back to False
        # (a real manual decision elsewhere must never be clobbered by
        # this running again), auto-approving on that trivial 0 would
        # permanently "stick" a false-positive approval that a later,
        # genuinely-over-buffer overtime could never correct.
        overtime_second = self.overtime_second or 0
        if 0 < overtime_second <= buffer_seconds:
            self.attendance_overtime_approve = True

    # def save(self, *args, **kwargs):
    #     self.update_attendance_overtime()
    #     self.attendance_day = EmployeeShiftDay.objects.get(
    #         day=self.attendance_date.strftime("%A").lower()
    #     )
    #     prev_attendance_approved = False
    #     self.adjust_minimum_hour()

    #     # Handle overtime cutoff and auto-approval
    #     self.handle_overtime_conditions()

    #     if self.pk is not None:
    #         # Get the previous values of the boolean field
    #         prev_state = Attendance.objects.get(pk=self.pk)
    #         prev_attendance_approved = prev_state.attendance_overtime_approve

    #     # super().save(*args, **kwargs)  #commend this line, it take too much time to complete
    #     employee_ot = self.employee_id.employee_overtime.filter(
    #         month=self.attendance_date.strftime("%B").lower(),
    #         year=self.attendance_date.year,
    #     ).first()
    #     if employee_ot:
    #         # Update if exists
    #         self.update_ot(employee_ot)
    #     else:
    #         # Create and update in one call
    #         employee_ot = self.create_ot()
    #         self.update_ot(employee_ot)
    #     approved = self.attendance_overtime_approve
    #     attendance_account = self.employee_id.employee_overtime.filter(
    #         month=self.attendance_date.strftime("%B").lower(),
    #         year=self.attendance_date.year,
    #     ).first()
    #     total_ot_seconds = attendance_account.overtime_second
    #     if approved and prev_attendance_approved is False:
    #         self.approved_overtime_second = self.overtime_second
    #         total_ot_seconds = total_ot_seconds + self.approved_overtime_second
    #     elif not approved:
    #         total_ot_seconds = total_ot_seconds - self.approved_overtime_second
    #         self.approved_overtime_second = 0
    #     attendance_account.overtime = format_time(total_ot_seconds)
    #     attendance_account.save()
    #     super().save(*args, **kwargs)
    #     self.first_save = False

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        old = None

        if not self.attendance_day:
            self.attendance_day = EmployeeShiftDay.objects.get(
                day=self.attendance_date.strftime("%A").lower()
            )

        if not is_new:
            old = Attendance.objects.only(
                "at_work_second",
                "approved_overtime_second",
                "minimum_hour",
                "attendance_overtime_approve",
            ).get(pk=self.pk)

            old_work = old.at_work_second or 0
            old_approved_ot = old.approved_overtime_second or 0
            old_approved_flag = old.attendance_overtime_approve or False

            old_min = strtime_seconds(old.minimum_hour)
            old_pending_today = max(0, old_min - old_work)
        else:
            old_work = 0
            old_approved_ot = 0
            old_pending_today = 0
            old_approved_flag = False

        self.update_attendance_overtime()
        self.adjust_minimum_hour()
        self.handle_overtime_conditions()

        if self.attendance_overtime_approve and not old_approved_flag:
            self.approved_overtime_second = self.overtime_second
        elif not self.attendance_overtime_approve:
            self.approved_overtime_second = 0
        else:
            self.approved_overtime_second = old_approved_ot

        new_work = self.at_work_second or 0
        new_approved_ot = self.approved_overtime_second or 0

        new_min = strtime_seconds(self.minimum_hour)
        new_pending_today = max(0, new_min - new_work)

        diff_work = new_work - old_work
        diff_approved_ot = new_approved_ot - old_approved_ot
        diff_pending = new_pending_today - old_pending_today

        super().save(*args, **kwargs)

        if diff_work == diff_approved_ot == diff_pending == 0:
            return

        month = self.attendance_date.strftime("%B").lower()
        year = self.attendance_date.year

        with transaction.atomic():
            ot, _ = AttendanceOverTime.objects.get_or_create(
                employee_id=self.employee_id,
                month=month,
                year=year,
                defaults={
                    "hour_account_second": 0,
                    "hour_pending_second": 0,
                    "overtime_second": 0,
                },
            )

            AttendanceOverTime.objects.filter(pk=ot.pk).update(
                hour_account_second=F("hour_account_second") + diff_work,
                overtime_second=F("overtime_second") + diff_approved_ot,
                hour_pending_second=F("hour_pending_second") + diff_pending,
            )

            ot.refresh_from_db(
                fields=["hour_account_second", "hour_pending_second", "overtime_second"]
            )
            ot.worked_hours = format_time(ot.hour_account_second or 0)
            ot.pending_hours = format_time(ot.hour_pending_second or 0)
            ot.overtime = format_time(ot.overtime_second or 0)
            ot.save(update_fields=["worked_hours", "pending_hours", "overtime"])

    def serialize(self):
        """
        Used to serialize attendance instance
        """
        # Return a dictionary containing the data you want to store
        # strftime("%d %b %Y") date
        # strftime("%I:%M %p") time
        serialized_data = {
            "employee_id": self.employee_id.id,
            "attendance_date": str(self.attendance_date),
            "attendance_clock_in_date": str(self.attendance_clock_in_date),
            "attendance_clock_in": str(self.attendance_clock_in),
            "attendance_clock_out": str(self.attendance_clock_out),
            "attendance_clock_out_date": str(self.attendance_clock_out_date),
            "shift_id": self.shift_id.id if self.shift_id else "",
            "work_type_id": self.work_type_id.id if self.work_type_id else "",
            "attendance_worked_hour": self.attendance_worked_hour,
            "minimum_hour": self.minimum_hour,
            "batch_attendance_id": (
                self.batch_attendance_id.id if self.batch_attendance_id else ""
            ),
            # Add other fields you want to store
        }
        return serialized_data

    def delete(self, *args, **kwargs):
        # Custom delete logic
        # Perform additional operations before deleting the object
        with contextlib.suppress(Exception):
            AttendanceActivity.objects.filter(
                attendance_date=self.attendance_date, employee_id=self.employee_id
            ).delete()
            employee_ot = self.employee_id.employee_overtime.filter(
                month=self.attendance_date.strftime("%B").lower(),
                year=self.attendance_date.strftime("%Y"),
            )
            if employee_ot.exists():
                self.update_ot(employee_ot.first())
        # Call the superclass delete() method to delete the object
        super().delete(*args, **kwargs)

        # Perform additional operations after deleting the object

    def create_ot(self):
        """
        Create a new Hours Balance instance if it doesn't exist for a specific month and year.
        Returns:
            AttendanceOverTime: The created or fetched AttendanceOverTime instance.
        """
        # Create or fetch the AttendanceOverTime instance
        employee_ot, created = AttendanceOverTime.objects.get_or_create(
            employee_id=self.employee_id,
            month=self.attendance_date.strftime("%B").lower(),
            year=self.attendance_date.year,
        )

        # Update only if the fields are available
        if self.attendance_overtime_approve:
            employee_ot.overtime = self.attendance_overtime

        if self.attendance_validated:
            employee_ot.hour_account = self.attendance_worked_hour

        employee_ot.save()
        return employee_ot

    def update_ot(self, employee_ot):
        """
        Update the hour account for the given employee.

        Args:
            employee_ot (obj): AttendanceOverTime instance
        """
        if apps.is_installed("leave"):
            approved_leave_requests = self.employee_id.leaverequest_set.filter(
                start_date__lte=self.attendance_date,
                end_date__gte=self.attendance_date,
                status="approved",
            )
        else:
            approved_leave_requests = []

        # Create exclude condition using Q objects
        exclude_condition = Q()
        if approved_leave_requests:
            # Combine multiple conditions for the exclude clause
            for leave in approved_leave_requests:
                exclude_condition |= Q(
                    attendance_date__range=(leave.start_date, leave.end_date)
                )

        # Filter month attendances in a single query
        month_attendances = (
            Attendance.objects.filter(
                employee_id=self.employee_id,
                attendance_date__month=self.attendance_date.month,
                attendance_date__year=self.attendance_date.year,
                attendance_validated=True,
            )
            .exclude(exclude_condition)
            .values("minimum_hour", "at_work_second")
        )

        # Calculate hour balance and hours pending in a single loop
        hour_balance = 0
        minimum_hour_second = 0
        for attendance in month_attendances:
            required_work_second = strtime_seconds(attendance["minimum_hour"])
            at_work_second = min(required_work_second, attendance["at_work_second"])
            hour_balance += at_work_second
            minimum_hour_second += required_work_second

        hours_pending = minimum_hour_second - hour_balance
        employee_ot.worked_hours = format_time(hour_balance)
        employee_ot.pending_hours = format_time(hours_pending)
        employee_ot.save()

        return employee_ot

    def clean(self, *args, **kwargs):
        super().clean(*args, **kwargs)
        now = datetime.now().time()
        today = datetime.today().date()

        # Convert to time if it's a string
        if isinstance(self.attendance_clock_out, str):
            out_time = datetime.strptime(self.attendance_clock_out, "%H:%M:%S").time()
        else:
            out_time = self.attendance_clock_out

        if (
            self.attendance_clock_in_date
            and self.attendance_clock_in_date < self.attendance_date
        ):
            raise ValidationError(
                {
                    "attendance_clock_in_date": "Attendance check-in date cannot be earlier than attendance date"
                }
            )

        if (
            self.attendance_clock_out_date
            and self.attendance_clock_in_date
            and self.attendance_clock_out_date < self.attendance_clock_in_date
        ):
            raise ValidationError(
                {
                    "attendance_clock_out_date": "Attendance check-out date cannot be earlier than check-in date"
                }
            )

        if self.attendance_clock_out_date and self.attendance_clock_out_date >= today:
            if out_time > now:
                raise ValidationError(
                    {"attendance_clock_out": "Check-out time cannot be in the future"}
                )


class AttendanceRequestFile(HorillaModel):
    file = models.FileField(upload_to=upload_path)


class AttendanceRequestComment(HorillaModel):
    """
    AttendanceRequestComment Model
    """

    request_id = models.ForeignKey(Attendance, on_delete=models.CASCADE)
    employee_id = models.ForeignKey(Employee, on_delete=models.CASCADE)
    files = models.ManyToManyField(AttendanceRequestFile, blank=True)
    comment = models.TextField(null=True, verbose_name=_("Comment"), max_length=255)

    def __str__(self) -> str:
        return f"{self.comment}"


class AttendanceOverTime(HorillaModel):
    """
    AttendanceOverTime model
    """

    employee_id = models.ForeignKey(
        Employee,
        on_delete=models.PROTECT,
        related_name="employee_overtime",
        verbose_name=_("Employee"),
    )
    month = models.CharField(
        max_length=10,
        verbose_name=_("Month"),
    )
    month_sequence = models.PositiveSmallIntegerField(default=0)
    year = models.CharField(
        default=datetime.now().strftime("%Y"),
        null=True,
        max_length=10,
        verbose_name=_("Year"),
    )
    worked_hours = models.CharField(
        max_length=10,
        default="00:00",
        null=True,
        validators=[validate_time_format],
        verbose_name=_("Worked Hours"),
    )
    pending_hours = models.CharField(
        max_length=10,
        default="00:00",
        null=True,
        validators=[validate_time_format],
        verbose_name=_("Pending Hours"),
    )
    overtime = models.CharField(
        max_length=20,
        default="00:00",
        validators=[validate_time_format],
        verbose_name=_("Overtime Hours"),
    )
    hour_account_second = models.IntegerField(
        default=0,
        null=True,
        verbose_name=_("Worked Seconds"),
    )
    hour_pending_second = models.IntegerField(
        default=0,
        null=True,
        verbose_name=_("Pending Seconds"),
    )
    overtime_second = models.IntegerField(
        default=0,
        null=True,
        verbose_name=_("Overtime Seconds"),
    )
    objects = HorillaCompanyManager(
        related_company_field="employee_id__employee_work_info__company_id"
    )

    class Meta:
        """
        Meta class to add some additional options
        """

        unique_together = [("employee_id"), ("month"), ("year")]
        ordering = ["-year", "-month_sequence"]
        verbose_name = _("Hours Balance")
        verbose_name_plural = _("Hours Balances")

    def __str__(self):
        return f"{self.employee_id} - {self.month}"

    def get_month_capitalized(self):
        """
        capitalize month
        """
        return self.month.capitalize()

    def edit_url_overtime(self):
        """
        Edit url
        """

        url = reverse("attendance-overtime-update", kwargs={"obj_id": self.pk})

        return url

    def delete_url_overtime(self):
        """
        delete url
        """

        url = reverse("attendance-overtime-delete", kwargs={"obj_id": self.pk})

        return url

    def hour_actions(self):
        """
        actions in hour account

        """

        return render_template(
            path="cbv/hour_account/hour_actions.html",
            context={"instance": self},
        )

    def hour_options(self):
        """
        options in hour account

        """

        return render_template(
            path="cbv/hour_account/hour_options.html",
            context={"instance": self},
        )

    def hour_account_subtitle(self):
        """
        Detail view subtitle
        """

        return f"{self.employee_id.get_department()} / {self.employee_id.get_job_position()}"

    def hour_account_detail(self):
        """
        detail view
        """

        url = reverse("hour-account-detail-view", kwargs={"pk": self.pk})

        return url

    def hour_detail_actions(self):
        """
        actions in hour account detail view

        """

        return render_template(
            path="cbv/hour_account/hour_detail_action.html",
            context={"instance": self},
        )

    def clean(self):
        try:
            year = int(self.year)
            if not (1900 <= year <= 2100):
                raise ValidationError(
                    {"year": _("Year must be an integer value between 1900 and 2100")}
                )
        except (ValueError, TypeError):
            raise ValidationError(
                {"year": _("Year must be an integer value between 1900 and 2100")}
            )

    def month_days(self):
        """
        this method is used to create new AttendanceOvertime's instance if there
        is no existing for a specific month and year
        """
        month = self.month_sequence + 1
        year = int(self.year)
        start_date = date(year, month, 1)
        if month == 12:
            end_date = date(year + 1, 1, 1) - timedelta(days=1)
        else:
            end_date = date(year, month + 1, 1) - timedelta(days=1)
        return start_date, end_date

    def not_validated_hrs(self):
        """
        This method will return not validated hours in a month
        """
        hrs_to_vlaidate = sum(
            list(
                Attendance.objects.filter(
                    attendance_date__month=MONTH_MAPPING[self.month],
                    attendance_date__year=self.year,
                    employee_id=self.employee_id,
                    attendance_validated=False,
                ).values_list("at_work_second", flat=True)
            )
        )
        return format_time(hrs_to_vlaidate)

    def not_approved_ot_hrs(self):
        """
        This method will return the overtime hours to be approved
        """
        hrs_to_approve = sum(
            list(
                Attendance.objects.filter(
                    attendance_date__month=MONTH_MAPPING[self.month],
                    attendance_date__year=self.year,
                    employee_id=self.employee_id,
                    attendance_validated=True,
                    attendance_overtime_approve=False,
                    overtime_second__isnull=False,
                ).values_list("overtime_second", flat=True)
            )
        )
        return format_time(hrs_to_approve)

    def get_month_index(self):
        """
        This method will return the index of the month
        """
        return MONTH_MAPPING[self.month]

    # def save(self, *args, **kwargs):
    #     self.hour_account_second = strtime_seconds(self.worked_hours)
    #     self.hour_pending_second = strtime_seconds(self.pending_hours)
    #     self.overtime_second = strtime_seconds(self.overtime)
    #     month_name = self.month.split("-")[0]
    #     months = [
    #         "january",
    #         "february",
    #         "march",
    #         "april",
    #         "may",
    #         "june",
    #         "july",
    #         "august",
    #         "september",
    #         "october",
    #         "november",
    #         "december",
    #     ]
    #     self.month_sequence = months.index(month_name)
    #     super().save(*args, **kwargs)

    def save(self, *args, **kwargs):
        self.worked_hours = format_time(self.hour_account_second or 0)
        self.pending_hours = format_time(self.hour_pending_second or 0)
        self.overtime = format_time(self.overtime_second or 0)

        month_name = self.month.split("-")[0]
        months = [
            "january",
            "february",
            "march",
            "april",
            "may",
            "june",
            "july",
            "august",
            "september",
            "october",
            "november",
            "december",
        ]
        self.month_sequence = months.index(month_name)

        super().save(*args, **kwargs)


class AttendanceLateComeEarlyOut(HorillaModel):
    """
    AttendanceLateComeEarlyOut model
    """

    choices = [
        ("late_come", _("Late Arrival")),
        ("early_out", _("Early Departure")),
        # Irregularities' Flexible-mode half: worked hours fell short of
        # AttendanceRuleSet.total_work_hours_reference. Lowercase to
        # match the two existing values in this same field, not the
        # newer "new choice fields are uppercase" convention -- that
        # rule is about brand-new fields, and one field mixing case
        # across its own values would be its own kind of inconsistency.
        ("flexible_shortfall", _("Flexible Hours Shortfall")),
    ]

    attendance_id = models.ForeignKey(
        Attendance,
        on_delete=models.PROTECT,
        related_name="late_come_early_out",
        verbose_name=_("Attendance"),
    )
    employee_id = models.ForeignKey(
        Employee,
        on_delete=models.DO_NOTHING,
        null=True,
        related_name="late_come_early_out",
        verbose_name=_("Employee"),
        editable=False,
    )
    type = models.CharField(max_length=20, choices=choices, verbose_name=_("Type"))
    objects = HorillaCompanyManager(
        related_company_field="employee_id__employee_work_info__company_id"
    )
    created_at = models.DateTimeField(auto_now_add=True, null=True)

    def get_penalties_count(self):
        """
        This method is used to return the total penalties in the late early instance
        """
        return self.penaltyaccounts_set.count()

    def save(self, *args, **kwargs) -> None:
        # Single save, not two -- a second super().save() call here used
        # to re-receive whatever **kwargs the caller passed (notably
        # force_insert=True from QuerySet.create()), forcing a second
        # INSERT of the same now-already-assigned pk and crashing with
        # a duplicate-key error. employee_id only reads attendance_id,
        # already set by every real caller before save() runs, so it
        # never needed the row to exist in the DB first.
        self.employee_id = self.attendance_id.employee_id
        super().save(*args, **kwargs)

    class Meta:
        """
        Meta class to add some additional options
        """

        unique_together = [("attendance_id"), ("type")]
        ordering = ["-attendance_id__attendance_date"]

    def get_type(self):
        """
        Display work type
        """
        return dict(self.choices).get(self.type)

    def penalities_column(self):
        """
        To get penalities

        """

        return render_template(
            path="cbv/late_come_and_early_out/penality.html",
            context={"instance": self},
        )

    def actions_column(self):
        """
        actions in hour account

        """

        return render_template(
            path="cbv/late_come_and_early_out/actions_column.html",
            context={"instance": self},
        )

    def detail_actions(self):
        """
        actions in hour account

        """

        return render_template(
            path="cbv/late_come_and_early_out/detail_action.html",
            context={"instance": self},
        )

    def late_come_subtitle(self):
        """
        Detail view subtitle
        """

        return f"{self.employee_id.get_department()} / {self.employee_id.get_job_position()}"

    def attendance_validated_check(self):
        if self.attendance_id.attendance_validated == True:
            return _("Yes")
        else:
            return _("No")

    def late_come_detail(self):
        """
        detail view
        """

        url = reverse("late-in-early-out-single-view", kwargs={"pk": self.pk})

        return url

    def __str__(self) -> str:
        return f"{self.attendance_id.employee_id.employee_first_name} \
            {self.attendance_id.employee_id.employee_last_name} - {self.type}"


class AttendanceValidationCondition(HorillaModel):
    """
    AttendanceValidationCondition model
    """

    validation_at_work = models.CharField(
        max_length=10,
        validators=[validate_time_format],
        verbose_name=_("Worked Hours Auto Approve Till"),
    )
    minimum_overtime_to_approve = models.CharField(
        blank=True, null=True, max_length=10, validators=[validate_time_format]
    )
    overtime_cutoff = models.CharField(
        blank=True, null=True, max_length=10, validators=[validate_time_format]
    )
    auto_approve_ot = models.BooleanField(
        default=False, verbose_name=_("Auto Approve OT")
    )
    company_id = models.ManyToManyField(Company, blank=True, verbose_name=_("Company"))
    objects = HorillaCompanyManager()

    def clean(self):
        """
        This method is used to perform some custom validations
        """
        super().clean()
        if not self.id and AttendanceValidationCondition.objects.exists():
            raise ValidationError(_("You cannot add more conditions."))

    def break_point_actions(self):
        """
        actions in hour account

        """

        return render_template(
            path="cbv/settings/break_point_action.html",
            context={"instance": self},
        )


class GraceTime(HorillaModel):
    """
    Model for saving Grace time
    """

    allowed_time = models.CharField(
        default="00:00:00",
        validators=[validate_hh_mm_ss_format],
        max_length=10,
        verbose_name=_("Allowed Time"),
    )
    allowed_time_in_secs = models.IntegerField()
    allowed_clock_in = models.BooleanField(
        default=True,
        help_text=_("Allcocate this grace time for Check-In Attendance"),
        verbose_name=_("Allowed Clock-In"),
    )
    allowed_clock_out = models.BooleanField(
        default=False,
        help_text=_("Allcocate this grace time for Check-Out Attendance"),
        verbose_name=_("Allowed Clock-Out"),
    )
    is_default = models.BooleanField(default=False)

    company_id = models.ManyToManyField(Company, blank=True, verbose_name=_("Company"))
    objects = HorillaCompanyManager()

    def __str__(self) -> str:
        return str(f"{self.allowed_time} - Hours")

    def get_instance_id(self):
        return self.id

    def is_active_col(self):
        """
        This method for get custome coloumn .
        """

        return render_template(
            path="cbv/settings/is_active_col_grace_time.html",
            context={"instance": self},
        )

    def allowed_time_col(self):
        """
        Allowed time col
        """
        return f"{self.allowed_time} Hours"

    def is_default_col(self):
        """
        Allowed time col
        """
        return _("Yes") if self.is_default else _("No")

    def action_col(self):
        """
        This method for get custome coloumn .
        """

        return render_template(
            path="cbv/settings/grace_time_default_action.html",
            context={"instance": self},
        )

    def applicable_on_clock_in_col(self):
        """
        This method for get custom column .
        """

        return render_template(
            path="cbv/settings/applicable_on_clock_in_col.html",
            context={"instance": self},
        )

    def applicable_on_clock_out_col(self):
        """
        This method for get custom column .
        """

        return render_template(
            path="cbv/settings/applicable_on_clock_out_col.html",
            context={"instance": self},
        )

    def get_shifts_display(self):
        """
        This method for get custom column .
        """

        return render_template(
            path="cbv/settings/grace_time_shift.html",
            context={"instance": self},
        )

    def clean(self):
        """
        This method is used to perform some custom validations
        """
        super().clean()
        if self.is_default:
            if GraceTime.objects.filter(is_default=True).exclude(id=self.id).exists():
                raise ValidationError(
                    _("There is already a default grace time that exists.")
                )

        allowed_time = self.allowed_time
        is_default = self.is_default
        exclude_default = not is_default

        if (
            GraceTime.objects.filter(allowed_time=allowed_time)
            .exclude(is_default=exclude_default)
            .exclude(id=self.id)
            .exists()
        ):
            raise ValidationError(
                {
                    "allowed_time": _(
                        "There is already an existing grace time with this allowed time."
                    )
                }
            )

    def save(self, *args, **kwargs):
        allowed_time = self.allowed_time
        hours, minutes, secs = allowed_time.split(":")

        hours_int = int(hours)
        minutes_int = int(minutes)
        secs_int = int(secs)

        hours_str = f"{hours_int:02d}"
        minutes_str = f"{minutes_int:02d}"
        secs_str = f"{secs_int:02d}"

        self.allowed_time = f"{hours_str}:{minutes_str}:{secs_str}"
        self.allowed_time_in_secs = hours_int * 3600 + minutes_int * 60 + secs_int
        super().save(*args, **kwargs)


class AttendanceGeneralSetting(HorillaModel):
    """
    AttendanceGeneralSettings
    """

    time_runner = models.BooleanField(default=True)
    enable_check_in = models.BooleanField(
        default=True,
        verbose_name=_("Enable Check in/Check out"),
        help_text=_(
            "Enabling this feature allows employees to record their attendance using the Check-In/Check-Out button."
        ),
    )
    # Company-wide master switch. Default True so existing per-shift
    # auto-punch-out behavior is unchanged unless an admin explicitly
    # turns it off -- when off, nothing here ever auto-closes a session
    # for this company (shift-based, Flexible-mode, or no-shift alike).
    auto_punch_out_enabled = models.BooleanField(
        default=True,
        verbose_name=_("Enable Auto Punch-out"),
        help_text=_(
            "Company-wide switch for Auto Punch-out. Turning this off "
            "overrides every other Auto Punch-out setting for this company."
        ),
    )
    # Flat cutoff for an employee with no shift assigned at all -- the
    # only case none of the tiered/shift-based settings can cover, since
    # both key off a shift schedule that doesn't exist for this employee.
    no_shift_auto_punch_out_time = models.TimeField(
        default=dt.time(23, 59),
        verbose_name=_("No-Shift Auto Punch-out Time"),
        help_text=_(
            "Cutoff time used to auto-close an open session for an "
            "employee with no shift assigned at all."
        ),
    )
    company_id = models.ForeignKey(Company, on_delete=models.CASCADE, null=True)
    objects = HorillaCompanyManager()

    def company_col(self):
        if self.company_id:
            return self.company_id.company
        else:
            return "All Company"

    def check_in_check_out_col(self):
        """
        This method for get custom coloumn .
        """

        return render_template(
            path="cbv/settings/check_in_check_out_col.html",
            context={"instance": self},
        )


class WorkRecords(models.Model):
    """
    WorkRecord Model
    """

    choices = [
        ("FDP", _("Present")),
        ("HDP", _("Half Day Present")),
        ("ABS", _("Absent")),
        ("HD", _("Holiday / Weekly Off")),
        ("CONF", _("Conflict")),
        ("DFT", _("Draft")),
    ]

    record_name = models.CharField(max_length=250, null=True, blank=True)
    work_record_type = models.CharField(max_length=10, null=True, choices=choices)
    employee_id = models.ForeignKey(
        Employee, on_delete=models.CASCADE, verbose_name=_("Employee")
    )
    date = models.DateField(null=True, blank=True)
    at_work = models.CharField(
        null=True,
        blank=True,
        validators=[
            validate_time_format,
        ],
        default="00:00",
        max_length=10,
    )  # 841
    min_hour = models.CharField(
        null=True,
        blank=True,
        validators=[
            validate_time_format,
        ],
        default="00:00",
        max_length=10,
    )
    at_work_second = models.IntegerField(null=True, blank=True, default=0)
    min_hour_second = models.IntegerField(null=True, blank=True, default=0)
    note = models.TextField(max_length=255)
    message = models.CharField(max_length=30, null=True, blank=True)
    is_attendance_record = models.BooleanField(default=False)
    attendance_id = models.ForeignKey(
        Attendance, on_delete=models.SET_NULL, blank=True, null=True
    )
    is_leave_record = models.BooleanField(default=False)
    if apps.is_installed("leave"):
        leave_request_id = models.ForeignKey(
            "leave.LeaveRequest",
            on_delete=models.SET_NULL,
            blank=True,
            null=True,
        )
    shift_id = models.ForeignKey(
        EmployeeShift, on_delete=models.SET_NULL, blank=True, null=True
    )
    day_percentage = models.FloatField(default=0)
    last_update = models.DateTimeField(null=True, blank=True)
    objects = HorillaCompanyManager("employee_id__employee_work_info__company_id")

    def title_message(self):
        title_message = self.message
        if title_message == "Leave":
            if apps.is_installed("leave"):
                title_message += f" | {self.leave_request_id.leave_type_id}"
        return title_message

    def save(self, *args, **kwargs):
        self.last_update = timezone.now()

        super().save(*args, **kwargs)

    def clean(self):
        super().clean()
        if not 0.0 <= self.day_percentage <= 1.0:
            raise ValidationError(_("Day percentage must be between 0.0 and 1.0"))

    def __str__(self):
        return (
            self.record_name
            if self.record_name is not None
            else f"{self.work_record_type}-{self.date}-{self.employee_id}"
        )

    class Meta:
        verbose_name = _("Daily Work Status")
        verbose_name_plural = _("Daily Work Status")
        constraints = [
            models.UniqueConstraint(
                fields=["employee_id", "date"],
                name="unique_work_record_per_employee_per_date",
            )
        ]


class AttendanceConflictResolution(HorillaModel):
    """
    HR decision for days where an attendance record overlaps a leave/holiday/week-off.
    resolution="attendance" → the day counts as attendance (leave/holiday ignored in summary).
    resolution="leave"      → the day counts as leave/holiday (attendance ignored in summary).
    """

    employee_id = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        related_name="conflict_resolutions",
        verbose_name=_("Employee"),
    )
    date = models.DateField(verbose_name=_("Date"))
    resolution = models.CharField(
        max_length=20,
        choices=[
            ("full_present", _("Full Present")),
            ("half_present", _("Half Day")),
            ("partial_hours", _("Partial Hours")),
            ("absent", _("Absent")),
            ("paid_leave", _("Paid Leave")),
            ("unpaid_leave", _("Unpaid Leave")),
            ("holiday", _("Holiday")),
            ("week_off", _("Week Off")),
            # legacy values kept for existing records
            ("attendance", _("Count as Attendance")),
            ("leave", _("Count as Leave / Holiday")),
        ],
        verbose_name=_("Resolution"),
    )
    conflict_type = models.CharField(
        max_length=20,
        blank=True,
        default="",
        verbose_name=_("Conflict Type"),
    )
    objects = HorillaCompanyManager("employee_id__employee_work_info__company_id")

    class Meta:
        unique_together = [["employee_id", "date"]]
        verbose_name = _("Attendance Conflict Resolution")
        verbose_name_plural = _("Attendance Conflict Resolutions")

    def __str__(self):
        return f"{self.employee_id} — {self.date} → {self.resolution}"


class AttendanceSummaryHours(HorillaModel):
    """
    Stores computed (or HR-overridden) total worked seconds for an employee
    over a specific date range.  Created/updated on every summary load;
    is_manually_edited=True records are never overwritten by the auto-compute.
    """

    employee_id = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        related_name="summary_hours",
        verbose_name=_("Employee"),
    )
    from_date = models.DateField(verbose_name=_("From Date"))
    to_date = models.DateField(verbose_name=_("To Date"))
    hours_second = models.IntegerField(
        default=0,
        verbose_name=_("Hours (seconds)"),
    )
    is_manually_edited = models.BooleanField(
        default=False,
        verbose_name=_("Manually Edited"),
    )

    objects = HorillaCompanyManager("employee_id__employee_work_info__company_id")

    class Meta:
        unique_together = [["employee_id", "from_date", "to_date"]]
        verbose_name = _("Attendance Summary Hours")
        verbose_name_plural = _("Attendance Summary Hours")

    def __str__(self):
        h, m = self.hours_second // 3600, (self.hours_second % 3600) // 60
        return f"{self.employee_id} {self.from_date}–{self.to_date}: {h}h{m:02d}m"


class AttendanceDailyHours(HorillaModel):
    """
    Per-employee per-date worked hours, editable inside the calendar modal.
    Created when a manager manually edits a single day's hours.
    When present with is_manually_edited=True, overrides the computed daily
    contribution in build_monthly_summary.
    """

    employee_id = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        related_name="daily_hours",
        verbose_name=_("Employee"),
    )
    date = models.DateField(verbose_name=_("Date"))
    hours_second = models.IntegerField(default=0, verbose_name=_("Hours (seconds)"))
    is_manually_edited = models.BooleanField(
        default=False,
        verbose_name=_("Manually Edited"),
    )
    modified_at = models.DateTimeField(auto_now=True, verbose_name=_("Modified At"))

    objects = HorillaCompanyManager("employee_id__employee_work_info__company_id")

    class Meta:
        unique_together = [["employee_id", "date"]]
        verbose_name = _("Attendance Daily Hours")
        verbose_name_plural = _("Attendance Daily Hours")

    def __str__(self):
        h, m = self.hours_second // 3600, (self.hours_second % 3600) // 60
        return f"{self.employee_id} {self.date}: {h}h{m:02d}m"


class PendingConfigChange(HorillaModel):
    """
    A scheduled-but-not-yet-applied change to a tiered config row (e.g. an
    AttendanceRuleSet). Every config change in this app -- editing an
    existing row or activating a brand-new one -- goes through this,
    never a direct field edit, so "changes apply from the 1st of next
    month" is one mechanism reused everywhere instead of being
    reimplemented per feature. See attendance/config_tiers.py's module
    docstring for the fuller design rationale.
    """

    STATUS_PENDING = "PENDING"
    STATUS_APPLIED = "APPLIED"
    STATUS_CANCELLED = "CANCELLED"
    STATUS_CHOICES = (
        (STATUS_PENDING, _("Pending")),
        (STATUS_APPLIED, _("Applied")),
        (STATUS_CANCELLED, _("Cancelled")),
    )

    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.PositiveIntegerField()
    target = GenericForeignKey("content_type", "object_id")

    tier = models.CharField(max_length=15, choices=TIER_CHOICES)
    # {field_name: new_value} / {field_name: old_value}. DjangoJSONEncoder
    # so Decimal/date/time/datetime values on the target don't raise at
    # save time -- they come back as plain strings on read, which is fine
    # for setattr()-then-save() in apply() below, just not round-trip
    # type-perfect.
    changes = models.JSONField(encoder=DjangoJSONEncoder)
    previous_values = models.JSONField(encoder=DjangoJSONEncoder)

    requested_by = models.ForeignKey(
        HorillaUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("Requested By"),
    )
    effective_date = models.DateField(db_index=True, verbose_name=_("Effective Date"))

    status = models.CharField(
        max_length=10, choices=STATUS_CHOICES, default=STATUS_PENDING
    )
    applied_at = models.DateTimeField(null=True, blank=True)

    objects = models.Manager()

    class Meta:
        indexes = [models.Index(fields=["status", "effective_date"])]
        verbose_name = _("Pending Config Change")
        verbose_name_plural = _("Pending Config Changes")

    def __str__(self):
        return f"{self.target} — pending change effective {self.effective_date}"

    @classmethod
    def schedule(cls, target, changes, requested_by=None, effective_date=None):
        """
        Create the pending change that will apply `changes` (a dict of
        field_name -> new_value) onto `target` on `effective_date`
        (defaults to the 1st of next month). If `target` already has a
        pending change, it's cancelled first -- last-write-wins, never
        stacked.
        """
        content_type = ContentType.objects.get_for_model(type(target))
        cls.objects.filter(
            content_type=content_type,
            object_id=target.pk,
            status=cls.STATUS_PENDING,
        ).update(status=cls.STATUS_CANCELLED)

        previous_values = {field: getattr(target, field) for field in changes}
        instance = cls.objects.create(
            content_type=content_type,
            object_id=target.pk,
            tier=getattr(target, "tier", ""),
            changes=changes,
            previous_values=previous_values,
            requested_by=requested_by,
            effective_date=effective_date or next_month_first(),
        )
        config_override_requested.send(sender=cls, instance=instance)
        return instance

    def apply(self):
        """
        Apply `changes` onto the target row and mark this change applied.
        Only called by the scheduler (attendance/scheduler.py); a no-op
        if already applied/cancelled, so re-running the scheduler is safe.

        Mutates the target row directly -- the row's own edit history
        (what it looked like before this) is preserved separately by its
        model's automatic history table (see AttendanceRuleSet.history,
        a HorillaAuditLog/django-simple-history field that snapshots
        every save() on its own), not by this method keeping old rows
        around. An open Attendance's attendance_rule_set FK still points
        at this same row after the edit -- what keeps an already-open
        session from seeing the new values mid-session is
        attendance_rule_set_snapshot, captured once at clock-in and never
        re-read from the live row afterward (see that field's comment).
        """
        if self.status != self.STATUS_PENDING:
            return
        with transaction.atomic():
            target = self.target
            if target is None:
                # Target row was deleted before this change ever applied.
                self.status = self.STATUS_CANCELLED
                self.save(update_fields=["status"])
                return
            old_mode = getattr(target, "mode", None)
            for field, value in self.changes.items():
                setattr(target, field, value)
            target.save()
            self.status = self.STATUS_APPLIED
            self.applied_at = timezone.now()
            self.save(update_fields=["status", "applied_at"])

            new_mode = getattr(target, "mode", None)
            if "mode" in self.changes and new_mode != old_mode:
                from attendance.activity_log import log_attendance_activity

                scope = getattr(target, "scope_display", None) or f"tier={target.tier}"
                log_attendance_activity(
                    actor=None,
                    action_type=AttendanceActivityLog.ACTION_ATTENDANCE_TYPE_SWITCH,
                    affected_employees=[],
                    what_changed=f"Attendance Type changed to {new_mode} for {scope}",
                    source="Attendance Rule Sets",
                )
        config_override_applied.send(sender=type(self), instance=self)

    def cancel(self):
        """Mark this change cancelled without applying it."""
        if self.status != self.STATUS_PENDING:
            return
        self.status = self.STATUS_CANCELLED
        self.save(update_fields=["status"])
        config_override_cancelled.send(sender=type(self), instance=self)


class AttendanceRuleSet(TieredConfigResolutionMixin, HorillaModel):
    """
    "This company's (or this department's, or this employee-type's)
    complete attendance rule set" -- the combined config record for
    Attendance Type, following the three-tier model (Company Default /
    Employee-Type Override / Department Override) in
    attendance/config_tiers.py.

    Deliberately one combined model, not one table per feature: the PRD
    describes Attendance Type, Validation Threshold, the Overtime cluster,
    and Regularization's enable/cap as configured and saved together, as
    one unit, at the same tiers -- so they share one record. Only
    Attendance Type's own fields are on this model for now; Validation/
    Overtime/Regularization fields land here via later migrations as
    those features are built, not as separate tables.
    """

    MODE_SHIFT_BASED = "SHIFT_BASED"
    MODE_FLEXIBLE = "FLEXIBLE"
    MODE_CHOICES = (
        (MODE_SHIFT_BASED, _("Shift-based")),
        (MODE_FLEXIBLE, _("Flexible")),
    )

    # Rule fields an Employee-Type override leaves blank to inherit from
    # the Company Default row -- per the PRD, an Employee-Type override
    # only ever picks the mode, never its own rule values. Auto-validate
    # is derived from the overtime auto-approve buffer fields already
    # listed here, not a separate setting -- see attendance_validate()
    # in attendance/views/views.py.
    RULE_FIELDS = (
        "mode",
        "late_grace_minutes",
        "auto_punch_out_cutoff_time",
        "total_work_hours_reference",
        "track_overtime",
        "ot_threshold_hours",
        "shift_ot_auto_approve_buffer_minutes",
        "flexible_ot_auto_approve_buffer_hours",
        "regularization_enabled",
        "regularization_monthly_cap",
        "irregularities_enabled",
    )
    INHERITED_FIELDS = (
        "late_grace_minutes",
        "auto_punch_out_cutoff_time",
        "total_work_hours_reference",
        "track_overtime",
        "ot_threshold_hours",
        "shift_ot_auto_approve_buffer_minutes",
        "flexible_ot_auto_approve_buffer_hours",
        "regularization_enabled",
        "regularization_monthly_cap",
        "irregularities_enabled",
    )

    tier = models.CharField(
        max_length=15,
        choices=TIER_CHOICES,
        default=TIER_COMPANY,
        verbose_name=_("Tier"),
    )
    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="attendance_rule_sets",
        verbose_name=_("Company"),
    )
    # Not a FK to a specific EmployeeType row -- EmployeeType names stay
    # free-form (a company can still have arbitrarily many, e.g. "Machine
    # Operator"), but the PRD's Employee-Type tier only ever has three
    # possible overrides. This resolves against EmployeeType.collar_category
    # instead (see TieredConfigResolutionMixin.resolve_for_employee()), so
    # every arbitrarily-named type still maps onto one of the three tiers.
    employee_type_category = models.CharField(
        max_length=15,
        choices=COLLAR_CATEGORY_CHOICES,
        null=True,
        blank=True,
        verbose_name=_("Employee Type"),
    )
    department = models.ForeignKey(
        Department,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="attendance_rule_sets",
        verbose_name=_("Department"),
    )
    # False on a brand-new override until its first PendingConfigChange
    # applies -- new overrides go through the same prospective-effective-
    # date delay as edits, one code path for both (see PendingConfigChange
    # .apply(), which flips this True as part of `changes`).
    is_active = models.BooleanField(default=True, verbose_name=_("Is Active"))
    # Starts at 1 on creation, incremented on every update after that
    # (see save() below) -- a quick, human-readable "which edit is this"
    # number to pair with AttendanceRuleSet.history, which has the actual
    # field-by-field detail for each of those versions but no single
    # number of its own to point at one.
    version = models.PositiveIntegerField(
        default=1, editable=False, verbose_name=_("Version")
    )

    mode = models.CharField(
        max_length=15,
        choices=MODE_CHOICES,
        default=MODE_SHIFT_BASED,
        verbose_name=_("Attendance Type"),
    )

    # Shift-based rules. Nullable (not default=0) even on Company Default
    # rows -- an INHERITED_FIELDS entry has to be able to genuinely store
    # "unset" so a non-Company-Default row can fall through to inherit it;
    # 0 is a legitimate real value for this field (no grace at all), so it
    # can't double as the "not set" sentinel.
    #
    # Fully wired at the model layer (RULE_FIELDS, INHERITED_FIELDS,
    # resolution, snapshotting) but not yet read anywhere -- late-mark
    # grace is still governed entirely by the pre-existing GraceTime
    # model (shift.grace_time_id, or the company-wide default GraceTime
    # row -- see late_come()/early_out() in attendance/views/
    # clock_in_out.py), a separate mechanism this field doesn't feed.
    # Deliberately excluded from the settings screen's form
    # (attendance/forms.py's ATTENDANCE_RULE_SET_EDITABLE_FIELDS) so an
    # admin can't set a value here that has no actual effect.
    late_grace_minutes = models.PositiveIntegerField(
        null=True, blank=True, verbose_name=_("Late-Mark Grace (minutes)")
    )

    auto_punch_out_cutoff_time = models.TimeField(
        null=True, blank=True, verbose_name=_("Flexible Auto Punch-out Cutoff")
    )
    total_work_hours_reference = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
        verbose_name=_("Total Work Hours Reference"),
    )
    # Overtime cluster. Whether OT is tracked/computed/approved at all
    # for this tier -- nullable so it's inheritable the same way as
    # everything else here; treated as "off" wherever it resolves blank,
    # so overtime is opt-in, not on by default.
    track_overtime = models.BooleanField(
        null=True, blank=True, verbose_name=_("Track Overtime")
    )
    # One shared duration, added to a mode-appropriate baseline to find
    # the point past which hours count as overtime (see Attendance.
    # update_attendance_overtime()):
    #   Shift-based: this day's actual shift end time (get_shift_end_
    #     time()) + ot_threshold_hours. E.g. shift ends 18:00, threshold
    #     1:30 -> overtime starts at 19:30.
    #   Flexible: total_work_hours_reference + ot_threshold_hours. E.g.
    #     reference 8 hours, threshold 1:30 -> overtime starts after
    #     9:30 worked.
    ot_threshold_hours = models.DecimalField(
        max_digits=4, decimal_places=2, null=True, blank=True,
        verbose_name=_("Overtime Threshold (hours)"),
    )
    # Auto-approve buffers, one per mode to match each mode's own OT-start
    # representation (a duration added to a clock time vs. a duration
    # added to a duration). Overtime within the buffer auto-approves
    # itself; beyond it, needs a manager's decision -- the inverted
    # comparison from what the pre-existing (now-replaced) auto-approve
    # logic did, see Attendance.handle_overtime_conditions().
    shift_ot_auto_approve_buffer_minutes = models.PositiveIntegerField(
        null=True, blank=True,
        verbose_name=_("Shift-based OT Auto-Approve Buffer (minutes)"),
    )
    flexible_ot_auto_approve_buffer_hours = models.DecimalField(
        max_digits=4, decimal_places=2, null=True, blank=True,
        verbose_name=_("Flexible OT Auto-Approve Buffer (hours)"),
    )

    # Regularization: whether an employee may raise a dispute at all for
    # this tier (opt-in, like track_overtime above), and how many they
    # may raise per calendar month -- counting every request raised,
    # rejected ones included, since RegularizationRequest never resets
    # once created (unlike the older, flag-based correction workflow).
    regularization_enabled = models.BooleanField(
        null=True, blank=True, verbose_name=_("Enable Regularization")
    )
    regularization_monthly_cap = models.PositiveIntegerField(
        null=True, blank=True, verbose_name=_("Regularization Monthly Cap")
    )

    # Irregularities: on/off switch for the Flexible-mode hours-shortfall
    # check (see flexible_shortfall() in attendance/views/clock_in_out.py).
    # The shift-based half (late arrivals/early departures) is pre-
    # existing, always-on behavior, unrelated to this flag -- this only
    # gates the new category being added. The PRD doesn't actually say
    # where this switch belongs on-screen; folding it into this combined
    # rule-set record matches the pattern everything else here follows,
    # but that's an assumption, not a confirmed placement -- see the
    # plan doc's open items.
    irregularities_enabled = models.BooleanField(
        null=True, blank=True, verbose_name=_("Enable Irregularities")
    )

    objects = models.Manager()
    # Automatic history table -- every save() (including PendingConfig
    # Change.apply()'s in-place edits) is snapshotted here on its own,
    # same pattern already used by Attendance/AttendanceActivity. This is
    # what answers "what did this row used to say," so apply() doesn't
    # need to keep old rows around itself.
    history = HorillaAuditLog(
        related_name="history_set",
        bases=[
            HorillaAuditInfo,
        ],
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["company"],
                condition=Q(tier=TIER_COMPANY),
                name="uniq_attendanceruleset_company_tier",
            ),
            models.UniqueConstraint(
                fields=["company", "employee_type_category"],
                condition=Q(tier=TIER_EMPLOYEE_TYPE),
                name="uniq_attendanceruleset_employee_type_tier",
            ),
            models.UniqueConstraint(
                fields=["company", "department"],
                condition=Q(tier=TIER_DEPARTMENT),
                name="uniq_attendanceruleset_department_tier",
            ),
        ]
        verbose_name = _("Attendance Rule Set")
        verbose_name_plural = _("Attendance Rule Sets")

    def clean(self):
        super().clean()
        if self.tier == TIER_COMPANY:
            if self.employee_type_category or self.department_id:
                raise ValidationError(
                    _(
                        "A Company Default row cannot have an employee type "
                        "or department set."
                    )
                )
        elif self.tier == TIER_EMPLOYEE_TYPE:
            if not self.employee_type_category or self.department_id:
                raise ValidationError(
                    _(
                        "An Employee-Type override must set an employee type "
                        "and leave department blank."
                    )
                )
        elif self.tier == TIER_DEPARTMENT:
            if not self.department_id or self.employee_type_category:
                raise ValidationError(
                    _(
                        "A Department override must set a department and "
                        "leave employee type blank."
                    )
                )
            if (
                self.company_id
                and self.department_id
                and not self.department.company_id.filter(pk=self.company_id).exists()
            ):
                raise ValidationError(
                    _("This department is not assigned to this company.")
                )

    def __str__(self):
        if self.tier == TIER_COMPANY:
            return f"{self.company} — Company Default"
        if self.tier == TIER_EMPLOYEE_TYPE:
            return f"{self.company} — {self.get_employee_type_category_display()} override"
        return f"{self.company} — {self.department} override"

    def save(self, *args, **kwargs):
        # self.pk is only set once this row already exists in the DB --
        # None on the very first save (a genuine creation, version stays
        # at its default of 1), set on every save after that (a real
        # update, bump it). Checked before super().save() writes the row,
        # not after, since that's the only point pk reliably tells the
        # two cases apart without an extra query.
        if self.pk is not None:
            self.version += 1
            # A partial save (update_fields=[...]) only ever writes the
            # fields listed -- without adding "version" here too, the
            # increment above would happen in memory and never reach the
            # database.
            update_fields = kwargs.get("update_fields")
            if update_fields is not None:
                kwargs["update_fields"] = set(update_fields) | {"version"}
        super().save(*args, **kwargs)

    @classmethod
    def capture_snapshot(cls, rule_set):
        """
        The raw data attendance_rule_set_snapshot freezes at clock-in:
        this row's OWN RULE_FIELDS values (whatever tier it is), plus --
        only when this row isn't already the Company Default itself --
        the Company Default row's own RULE_FIELDS values too, for
        resolve_effective_value() to fall back to. Returns None if
        `rule_set` is None (no rule set configured at all).

        Captured as two separate raw dicts, not one pre-merged dict, so
        it's always visible afterward exactly what this specific row set
        itself versus what it borrowed from the company -- and so the
        actual own-wins-else-inherit resolution can be redone later
        (e.g. at checkout) purely from this frozen data, without ever
        touching the live rows again.
        """
        if rule_set is None:
            return None
        own = {name: getattr(rule_set, name) for name in cls.RULE_FIELDS}
        company_default_values = None
        if rule_set.tier != TIER_COMPANY:
            company_default = cls.objects.filter(
                tier=TIER_COMPANY, company=rule_set.company, is_active=True,
            ).first()
            if company_default is not None:
                company_default_values = {
                    name: getattr(company_default, name) for name in cls.RULE_FIELDS
                }
        return {"own": own, "company_default": company_default_values}

    @classmethod
    def resolve_effective_value(cls, snapshot, field_name):
        """
        One field's effective value, resolved purely from a frozen
        attendance_rule_set_snapshot dict (see capture_snapshot() above)
        -- never from a live row. This is what actually keeps an
        already-open session's resolved values from drifting: the row
        this snapshot came from, or the Company Default it borrowed
        from, may both have been edited since, but this function never
        looks at them again. Mirrors get_effective_values()'s own-value-
        wins-else-inherit-if-the-field-is-inheritable logic exactly.
        """
        if not snapshot:
            return None
        own_value = (snapshot.get("own") or {}).get(field_name)
        if own_value not in (None, "") or field_name not in cls.INHERITED_FIELDS:
            return own_value
        company_default_values = snapshot.get("company_default") or {}
        return company_default_values.get(field_name)


class RegularizationRequest(HorillaModel):
    """
    An employee's durable dispute over a flagged/closed attendance day --
    a corrected time, an auto-close, a geo-location flag, or a denied
    overtime decision -- routed to their manager (or an approval
    delegate, see ApprovalDelegate below) for a final approve/reject.

    Deliberately separate from the older is_validate_request/
    requested_data flag-based workflow on Attendance itself: that flag
    resets to blank the moment it's resolved (approve OR reject), even
    deleting the whole Attendance row for a rejected create_request --
    see cancel_attendance_request() in attendance/views/requests.py. That
    makes it structurally unable to do two things this feature needs:
    count every request raised toward a monthly cap (rejected ones
    included, so they have to survive resolution), and cover dispute
    types (auto-close, geo-flag, OT denial) that flag was never designed
    to represent at all. The old workflow stays exactly as-is for
    ordinary attendance edits -- this is additive, not a replacement.
    """

    REASON_TIME_CORRECTION = "TIME_CORRECTION"
    REASON_AUTO_CLOSE_DISPUTE = "AUTO_CLOSE_DISPUTE"
    REASON_GEO_VIOLATION_DISPUTE = "GEO_VIOLATION_DISPUTE"
    REASON_OVERTIME_DENIAL_DISPUTE = "OVERTIME_DENIAL_DISPUTE"
    REASON_CHOICES = (
        (REASON_TIME_CORRECTION, _("Time Correction")),
        (REASON_AUTO_CLOSE_DISPUTE, _("Auto Punch-out Dispute")),
        (REASON_GEO_VIOLATION_DISPUTE, _("Geo-location Dispute")),
        (REASON_OVERTIME_DENIAL_DISPUTE, _("Overtime Decision Dispute")),
    )

    STATUS_PENDING = "PENDING"
    STATUS_APPROVED = "APPROVED"
    STATUS_REJECTED = "REJECTED"
    STATUS_CHOICES = (
        (STATUS_PENDING, _("Pending")),
        (STATUS_APPROVED, _("Approved")),
        (STATUS_REJECTED, _("Rejected")),
    )

    # Independent of `status` above -- only meaningful when this dispute
    # involves overtime. Same "two independent decisions" shape as the
    # Overtime feature itself (Attendance.attendance_overtime_approve is
    # its own decision, separate from attendance_validated) -- approving
    # the main request and approving its overtime piece are genuinely
    # separate outcomes, not one flag wearing two hats.
    OT_DECISION_PENDING = "PENDING"
    OT_DECISION_APPROVED = "APPROVED"
    OT_DECISION_DENIED = "DENIED"
    OT_DECISION_CHOICES = (
        (OT_DECISION_PENDING, _("Pending")),
        (OT_DECISION_APPROVED, _("Approved")),
        (OT_DECISION_DENIED, _("Denied")),
    )

    employee = models.ForeignKey(
        Employee, on_delete=models.CASCADE,
        related_name="regularization_requests", verbose_name=_("Employee"),
    )
    attendance = models.ForeignKey(
        "attendance.Attendance", on_delete=models.CASCADE,
        related_name="regularization_requests", verbose_name=_("Attendance"),
    )
    reason_code = models.CharField(
        max_length=25, choices=REASON_CHOICES, verbose_name=_("Reason")
    )
    reason = models.TextField(verbose_name=_("Explanation"))

    # Only meaningful for reason_code=TIME_CORRECTION.
    corrected_clock_in = models.TimeField(null=True, blank=True)
    corrected_clock_in_date = models.DateField(null=True, blank=True)
    corrected_clock_out = models.TimeField(null=True, blank=True)
    corrected_clock_out_date = models.DateField(null=True, blank=True)

    status = models.CharField(
        max_length=10, choices=STATUS_CHOICES, default=STATUS_PENDING,
    )
    overtime_decision = models.CharField(
        max_length=10, choices=OT_DECISION_CHOICES, null=True, blank=True,
    )

    resolved_by = models.ForeignKey(
        Employee, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name=_("Resolved By"),
    )
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolution_note = models.TextField(null=True, blank=True)

    objects = models.Manager()

    class Meta:
        verbose_name = _("Regularization Request")
        verbose_name_plural = _("Regularization Requests")

    def __str__(self):
        return (
            f"{self.employee} — {self.get_reason_code_display()} "
            f"({self.attendance.attendance_date})"
        )

    def clean(self):
        super().clean()
        if (
            self.reason_code == self.REASON_TIME_CORRECTION
            and not self.corrected_clock_in
            and not self.corrected_clock_out
        ):
            raise ValidationError(
                _(
                    "A time correction request must set a corrected "
                    "clock-in and/or clock-out."
                )
            )

    @classmethod
    def monthly_count(cls, employee, month, year):
        """
        Every request raised in that calendar month, regardless of
        status -- rejected ones count too, unlike the old flag-based
        workflow this replaces for Regularization's own scope. Counted
        by when the request was *raised* (created_at), not the date of
        the attendance it disputes.
        """
        return cls.objects.filter(
            employee=employee, created_at__year=year, created_at__month=month,
        ).count()

    @classmethod
    def can_raise(cls, employee):
        """
        (allowed, reason_if_not) -- whether `employee` may raise a new
        request right now. Opt-in like track_overtime: a tier that never
        configured regularization_enabled is treated as disabled, not
        open by default.
        """
        rule_set = AttendanceRuleSet.resolve_for_employee(employee)
        effective = rule_set.get_effective_values() if rule_set is not None else {}
        if not effective.get("regularization_enabled"):
            return False, _("Regularization is not enabled for you.")
        cap = effective.get("regularization_monthly_cap")
        if cap is not None:
            today = date.today()
            if cls.monthly_count(employee, today.month, today.year) >= cap:
                return False, _(
                    "You have reached your monthly regularization request limit."
                )
        return True, None

    def approve(self, resolved_by, resolution_note=""):
        """
        Resolves the dispute in the employee's favor. What that actually
        changes on the underlying Attendance day depends on reason_code
        -- a no-op if already resolved, so this is safe to call from a
        retried request.
        """
        if self.status != self.STATUS_PENDING:
            return
        with transaction.atomic():
            if self.reason_code == self.REASON_TIME_CORRECTION:
                self._apply_time_correction()
            elif self.reason_code == self.REASON_GEO_VIOLATION_DISPUTE:
                self.attendance.geo_fence_violation = False
                self.attendance.geo_fence_unverified = False
                self.attendance.save()
            elif self.reason_code == self.REASON_OVERTIME_DENIAL_DISPUTE:
                self.attendance.attendance_overtime_approve = True
                self.attendance.save()
                self.overtime_decision = self.OT_DECISION_APPROVED
            # REASON_AUTO_CLOSE_DISPUTE: no dedicated flag exists on
            # Attendance yet to clear (Auto Punch-out doesn't record one
            # today) -- approving is still recorded here for visibility/
            # history, it just has no further automated side effect yet.

            self.status = self.STATUS_APPROVED
            self.resolved_by = resolved_by
            self.resolved_at = timezone.now()
            self.resolution_note = resolution_note
            self.save()

        from attendance.activity_log import log_attendance_activity

        log_attendance_activity(
            actor=resolved_by,
            action_type=AttendanceActivityLog.ACTION_REGULARIZATION_DECISION,
            affected_employees=self.employee,
            what_changed=f"Regularization request approved ({self.get_reason_code_display()})",
            source="Regularization",
        )

    def reject(self, resolved_by, resolution_note=""):
        """
        A no-op if already resolved. Rejecting the main request forces
        any overtime piece to denied too, regardless of what it might
        otherwise have been on its way to -- the two decisions are
        independent, but a rejected dispute can't leave its overtime
        half dangling as still-pending.
        """
        if self.status != self.STATUS_PENDING:
            return
        self.status = self.STATUS_REJECTED
        if self.reason_code == self.REASON_OVERTIME_DENIAL_DISPUTE:
            self.overtime_decision = self.OT_DECISION_DENIED
        self.resolved_by = resolved_by
        self.resolved_at = timezone.now()
        self.resolution_note = resolution_note
        self.save()

        from attendance.activity_log import log_attendance_activity

        log_attendance_activity(
            actor=resolved_by,
            action_type=AttendanceActivityLog.ACTION_REGULARIZATION_DECISION,
            affected_employees=self.employee,
            what_changed=f"Regularization request rejected ({self.get_reason_code_display()})",
            source="Regularization",
        )

    def _apply_time_correction(self):
        attendance = self.attendance
        if self.corrected_clock_in:
            attendance.attendance_clock_in = self.corrected_clock_in
        if self.corrected_clock_in_date:
            attendance.attendance_clock_in_date = self.corrected_clock_in_date
        if self.corrected_clock_out:
            attendance.attendance_clock_out = self.corrected_clock_out
        if self.corrected_clock_out_date:
            attendance.attendance_clock_out_date = self.corrected_clock_out_date
        if self.corrected_clock_in and self.corrected_clock_out:
            in_dt = datetime.combine(
                self.corrected_clock_in_date or attendance.attendance_date,
                self.corrected_clock_in,
            )
            out_dt = datetime.combine(
                self.corrected_clock_out_date or attendance.attendance_date,
                self.corrected_clock_out,
            )
            worked_seconds = max(0, int((out_dt - in_dt).total_seconds()))
            attendance.attendance_worked_hour = format_time(worked_seconds)
        attendance.save()


class ApprovalDelegate(HorillaModel):
    """
    A manager handing off Validation/Overtime/Regularization approval
    authority to someone else -- shared infrastructure across all three
    approval flows, not specific to Regularization even though that's
    the feature that surfaced the need for it. Either a whole date range
    (e.g. covering the manager's own leave) or one specific request, not
    both -- see clean(). Only an employee holding the dedicated
    attendance.can_be_delegate permission (separate from ordinary
    approval rights like attendance.change_attendance) may be picked.
    """

    delegator = models.ForeignKey(
        Employee, on_delete=models.CASCADE,
        related_name="delegations_given", verbose_name=_("Delegator"),
    )
    delegate = models.ForeignKey(
        Employee, on_delete=models.CASCADE,
        related_name="delegations_received", verbose_name=_("Delegate"),
    )

    start_date = models.DateField(null=True, blank=True, verbose_name=_("Start Date"))
    end_date = models.DateField(null=True, blank=True, verbose_name=_("End Date"))

    # Narrows this delegation to exactly one request instead of a date
    # range. Generic, not a plain FK to RegularizationRequest, since the
    # same delegation mechanism is meant to cover Validation and
    # Overtime approval targets too, without this model needing to know
    # about those apps' specific target types.
    content_type = models.ForeignKey(
        ContentType, on_delete=models.CASCADE, null=True, blank=True,
    )
    object_id = models.PositiveIntegerField(null=True, blank=True)
    target = GenericForeignKey("content_type", "object_id")

    is_active = models.BooleanField(default=True, verbose_name=_("Is Active"))

    objects = models.Manager()

    class Meta:
        verbose_name = _("Approval Delegate")
        verbose_name_plural = _("Approval Delegates")
        permissions = [
            ("can_be_delegate", "Can be selected as an approval delegate"),
        ]

    def __str__(self):
        if self.object_id:
            return f"{self.delegator} → {self.delegate} (one request)"
        return f"{self.delegator} → {self.delegate} ({self.start_date}–{self.end_date})"

    def clean(self):
        super().clean()
        if self.delegator_id and self.delegate_id and self.delegator_id == self.delegate_id:
            raise ValidationError(_("A manager cannot delegate to themselves."))
        has_target = bool(self.object_id)
        has_range = bool(self.start_date or self.end_date)
        if not has_target and not has_range:
            raise ValidationError(
                _("Set either a specific request or a start/end date range.")
            )
        if has_target and has_range:
            raise ValidationError(
                _("Set either a specific request or a date range, not both.")
            )
        if has_range and not (self.start_date and self.end_date):
            raise ValidationError(_("A date-range delegation needs both dates set."))
        if has_range and self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValidationError(_("Start date must be before end date."))
        if self.delegate_id:
            user = getattr(self.delegate, "employee_user_id", None)
            if user is None or not user.has_perm("attendance.can_be_delegate"):
                raise ValidationError(
                    _("This employee is not permitted to act as a delegate.")
                )

    def covers(self, on_date=None, target=None):
        """
        True if this delegation currently authorizes approving `target`
        (a specific model instance), or, for a date-range delegation,
        covers `on_date` (defaults to today).
        """
        if not self.is_active:
            return False
        if self.object_id is not None:
            if target is None:
                return False
            return (
                ContentType.objects.get_for_model(type(target)) == self.content_type
                and target.pk == self.object_id
            )
        on_date = on_date or date.today()
        return self.start_date <= on_date <= self.end_date

    @classmethod
    def is_delegate_for(cls, delegate_employee, delegator_employee, on_date=None, target=None):
        """
        True if `delegate_employee` currently stands in for
        `delegator_employee`'s approval authority, for `target` (a
        specific request) or `on_date` (defaults to today).
        """
        candidates = cls.objects.filter(
            delegator=delegator_employee, delegate=delegate_employee, is_active=True,
        )
        return any(d.covers(on_date=on_date, target=target) for d in candidates)

    @classmethod
    def can_approve(cls, approver, target_employee, on_date=None, target=None):
        """
        True if `approver` may approve/reject something belonging to
        `target_employee` -- either as their direct reporting manager,
        or as an active delegate standing in for that manager.
        """
        if approver == target_employee:
            return False
        reporting_manager = target_employee.get_reporting_manager()
        if reporting_manager is None:
            return False
        if reporting_manager == approver:
            return True
        return cls.is_delegate_for(
            approver, reporting_manager, on_date=on_date, target=target,
        )


class AttendanceActivityLog(HorillaModel):
    """
    A single, unified, second-level-precision record of every
    attendance-related action -- raw punches and every admin/system
    action that creates, changes, or decides on an attendance record
    alike (see the "Attendance Activity Log" PRD section). Deliberately
    NOT built on django-auditlog/horilla_audit: that mechanism is
    generic field-diff logging, one row per object per save, with no
    concept of "affected employee" distinct from "which row changed" --
    neither fits this model's two mandatory facets (Actor, Affected
    Employee(s)) or its batch-action requirement (one row covering many
    employees, not one row per employee). Rows are written explicitly,
    at the point of action, via log_attendance_activity()
    (attendance/activity_log.py) -- not captured automatically by a
    signal -- so every action type this covers has its own deliberate
    call site, not a generic hook.

    affected_employee_ids is always a list, even for a single-employee
    action (a 1-element list) -- this is what lets one model, one table,
    cover both the single-record case and the batch case (Bulk Import,
    Batch Entry Create Attendance) without needing a second model the
    way an auditlog-based design would have.
    """

    ACTION_PUNCH_IN = "PUNCH_IN"
    ACTION_PUNCH_OUT = "PUNCH_OUT"
    ACTION_AUTO_PUNCH_OUT = "AUTO_PUNCH_OUT"
    ACTION_ATTENDANCE_TYPE_SWITCH = "ATTENDANCE_TYPE_SWITCH"
    ACTION_VALIDATION_AUTO_PASS = "VALIDATION_AUTO_PASS"
    ACTION_OVERTIME_AUTO_APPROVE = "OVERTIME_AUTO_APPROVE"
    ACTION_MANUAL_CREATE_OVERRIDE = "MANUAL_CREATE_OVERRIDE"
    ACTION_REGULARIZATION_DECISION = "REGULARIZATION_DECISION"
    ACTION_OVERTIME_MANUAL_DECISION = "OVERTIME_MANUAL_DECISION"
    ACTION_BULK_IMPORT = "BULK_IMPORT"
    ACTION_CHOICES = (
        (ACTION_PUNCH_IN, _("Punch In")),
        (ACTION_PUNCH_OUT, _("Punch Out")),
        (ACTION_AUTO_PUNCH_OUT, _("Auto Punch-out")),
        (ACTION_ATTENDANCE_TYPE_SWITCH, _("Attendance Type Switch")),
        (ACTION_VALIDATION_AUTO_PASS, _("Validation Auto-Pass")),
        (ACTION_OVERTIME_AUTO_APPROVE, _("Overtime Auto-Approve")),
        (ACTION_MANUAL_CREATE_OVERRIDE, _("Manual Create/Override")),
        (ACTION_REGULARIZATION_DECISION, _("Regularization Decision")),
        (ACTION_OVERTIME_MANUAL_DECISION, _("Overtime Manual Decision")),
        (ACTION_BULK_IMPORT, _("Bulk Import")),
    )

    timestamp = models.DateTimeField(auto_now_add=True, verbose_name=_("Timestamp"))
    # Null means System -- an automated job (Auto Punch-out, a
    # threshold-based auto-pass/auto-approve), not a missing/unknown
    # actor. Never SET_NULL'd away from a real actor after the fact --
    # PROTECT keeps that distinction honest; an employee record with
    # activity history against it can't be hard-deleted out from under
    # its own log entries.
    actor = models.ForeignKey(
        Employee, on_delete=models.PROTECT, null=True, blank=True,
        related_name="attendance_activity_actions", verbose_name=_("Actor"),
    )
    action_type = models.CharField(
        max_length=30, choices=ACTION_CHOICES, verbose_name=_("Action Type")
    )
    affected_employee_ids = models.JSONField(default=list, verbose_name=_("Affected Employees"))
    what_changed = models.CharField(max_length=255, verbose_name=_("What Changed"))
    source = models.CharField(max_length=50, verbose_name=_("Source"))

    class Meta:
        verbose_name = _("Attendance Activity Log")
        verbose_name_plural = _("Attendance Activity Logs")
        ordering = ["-timestamp"]

    def __str__(self):
        return f"{self.get_action_type_display()} by {self.actor_display} ({self.timestamp})"

    @property
    def actor_display(self):
        return str(self.actor) if self.actor else _("System")

    @property
    def affected_employee_count(self):
        return len(self.affected_employee_ids or [])

    def affected_employees(self):
        return Employee.objects.filter(pk__in=self.affected_employee_ids or [])


class PayrollReadinessSnapshot(HorillaModel):
    """
    Payroll Readiness's "Final Report" -- a frozen, one-time export of a
    pay period, generated on manual "Lock Forever" (no automatic pay-
    group-cutoff trigger exists yet, since no pay-group/cutoff concept
    exists anywhere in payroll/ -- same "obvious extension point, not a
    blocker" treatment as Create Attendance's own payroll-cutoff stub).

    "Locked forever" is the one real behavioral precedent-setter in
    this feature -- nothing else in this codebase is designed to be
    permanently frozen after the fact. Enforced two ways: the unique
    constraint below blocks a second lock of the exact same period
    outright (never silently overwritten), and nothing anywhere ever
    updates/deletes a row on this model or its PayrollReadinessSnapshotRow
    children once created.

    Per the PRD's own rule for this step, confirmed explicitly rather
    than assumed: a day still carrying an open Exception (pending
    Validation, Overtime, or Regularization) at the moment of locking
    goes in zeroed out, permanently, for that cycle -- it does NOT block
    the lock. See PayrollReadinessSnapshotRow.was_exception.
    """

    company = models.ForeignKey(
        Company, on_delete=models.PROTECT, verbose_name=_("Company")
    )
    start_date = models.DateField(verbose_name=_("Period Start"))
    end_date = models.DateField(verbose_name=_("Period End"))
    locked_by = models.ForeignKey(
        Employee, on_delete=models.SET_NULL, null=True, verbose_name=_("Locked By")
    )
    locked_at = models.DateTimeField(auto_now_add=True, verbose_name=_("Locked At"))

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["company", "start_date", "end_date"],
                name="unique_payroll_readiness_period",
            )
        ]
        ordering = ["-start_date"]

    def __str__(self):
        return f"{self.company} ({self.start_date} - {self.end_date})"


class PayrollReadinessSnapshotRow(HorillaModel):
    """
    One employee's one day, frozen at lock time -- the Final Report
    reads only these rows, never live WorkRecords/Attendance data,
    which is what actually makes the snapshot immutable (the source
    data it was built from can keep changing after the fact; this copy
    can't).
    """

    DAY_WORKED = "WORKED"
    DAY_HOLIDAY = "HOLIDAY"
    DAY_WEEKLY_OFF = "WEEKLY_OFF"
    DAY_APPROVED_LEAVE = "APPROVED_LEAVE"
    DAY_EXCEPTION = "EXCEPTION"
    DAY_TYPE_CHOICES = (
        (DAY_WORKED, _("Worked")),
        (DAY_HOLIDAY, _("Holiday")),
        (DAY_WEEKLY_OFF, _("Weekly-Off")),
        (DAY_APPROVED_LEAVE, _("Approved Leave")),
        (DAY_EXCEPTION, _("Exception")),
    )

    snapshot = models.ForeignKey(
        PayrollReadinessSnapshot, on_delete=models.CASCADE, related_name="rows"
    )
    employee = models.ForeignKey(
        Employee, on_delete=models.PROTECT, verbose_name=_("Employee")
    )
    date = models.DateField(verbose_name=_("Date"))
    day_type = models.CharField(
        max_length=20, choices=DAY_TYPE_CHOICES, verbose_name=_("Day Type")
    )
    check_in = models.TimeField(null=True, blank=True, verbose_name=_("Check-in"))
    check_out = models.TimeField(null=True, blank=True, verbose_name=_("Check-out"))
    worked_hours = models.CharField(max_length=10, default="00:00")
    overtime_hours = models.CharField(max_length=10, default="00:00")
    # True means this day's hours were zeroed out because it still had
    # an open Exception at the moment this snapshot was locked -- kept
    # as its own flag (not inferred from day_type==EXCEPTION, since a
    # day can be an Exception for reasons that don't zero its hours in
    # every future scenario) so the Final Report can call this out
    # explicitly rather than making the reader infer it from zero hours
    # alone, which could just as easily mean a real, legitimate zero.
    was_exception = models.BooleanField(default=False)

    class Meta:
        ordering = ["employee_id", "date"]

    def __str__(self):
        return f"{self.employee} - {self.date} ({self.day_type})"


class BackgroundAttendanceTask(HorillaModel):
    """
    What makes deferring late-come/early-out flagging (Part 3 of the
    Attendance performance plan) safe to fail and retry: the fact that a
    punch still needs this processing is written durably here,
    synchronously, at punch time -- a Celery worker (attendance/tasks.py)
    reads this table to know what still needs doing, rather than the
    queue being the only record of it.

    Deferring the monthly overtime account update (Attendance.save()'s
    AttendanceOverTime block) the same way was investigated and declined
    -- measured at ~1.35ms/save, not worth the payroll-correctness risk
    of a window where that total hasn't caught up yet. `kind` only has
    one value as a result; the field stays a CharField+choices rather
    than being collapsed to a boolean so a genuinely different kind of
    deferred work can still be added later without a schema change.
    """

    KIND_LATE_COME_EARLY_OUT = "LATE_COME_EARLY_OUT"
    KIND_CHOICES = (
        (KIND_LATE_COME_EARLY_OUT, _("Late-Come / Early-Out Flagging")),
    )

    STATUS_PENDING = "PENDING"
    STATUS_PROCESSING = "PROCESSING"
    STATUS_SUCCESS = "SUCCESS"
    STATUS_FAILED = "FAILED"
    STATUS_CHOICES = (
        (STATUS_PENDING, _("Pending")),
        (STATUS_PROCESSING, _("Processing")),
        (STATUS_SUCCESS, _("Success")),
        (STATUS_FAILED, _("Failed")),
    )

    # Automatic retries -- all of them run through the periodic sweep
    # (see attendance/tasks.py's module docstring for why Celery's own
    # self.retry() isn't used) -- stop once attempts reaches this. The
    # row stays visible either way; only automatic retry stops.
    # settings.MAX_RETRIES is env-overridable (horilla/settings/base.py).
    MAX_ATTEMPTS = settings.MAX_RETRIES

    attendance = models.ForeignKey(
        "attendance.Attendance", on_delete=models.CASCADE,
        related_name="background_tasks", verbose_name=_("Attendance"),
    )
    kind = models.CharField(max_length=25, choices=KIND_CHOICES, verbose_name=_("Kind"))
    status = models.CharField(
        max_length=10, choices=STATUS_CHOICES, default=STATUS_PENDING,
    )
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.TextField(null=True, blank=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    objects = models.Manager()

    class Meta:
        verbose_name = _("Background Attendance Task")
        verbose_name_plural = _("Background Attendance Tasks")

    def __str__(self):
        return f"{self.get_kind_display()} for {self.attendance} ({self.status})"


class BackgroundAttendanceTaskRetryLog(HorillaModel):
    """
    Append-only: one row per manual reset of a BackgroundAttendanceTask
    that ran out of automatic retries. The task itself always reflects
    only its current attempt cycle (reset back to attempts=0 on a manual
    retry); the history of every past manual intervention lives here
    instead, so it isn't lost just because the task row gets reused
    rather than replaced.
    """

    task = models.ForeignKey(
        BackgroundAttendanceTask, on_delete=models.CASCADE,
        related_name="retry_log", verbose_name=_("Task"),
    )
    reset_by = models.ForeignKey(
        Employee, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name=_("Reset By"),
    )
    reset_at = models.DateTimeField(auto_now_add=True)
    previous_status = models.CharField(max_length=10)
    previous_attempts = models.PositiveIntegerField()
    note = models.TextField(null=True, blank=True)

    objects = models.Manager()

    class Meta:
        verbose_name = _("Background Attendance Task Retry Log")
        verbose_name_plural = _("Background Attendance Task Retry Logs")

    def __str__(self):
        return f"Retry of {self.task_id} by {self.reset_by} at {self.reset_at}"
