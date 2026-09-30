"""
Tests for Attendance.update_attendance_overtime() / handle_overtime_
conditions() -- the Overtime feature (#7): opt-in tracking per tier, one
shared company-level ot_threshold_hours added to a mode-appropriate
baseline to find where overtime starts (shift end time for Shift-based,
total_work_hours_reference for Flexible -- see AttendanceRuleSet.
ot_threshold_hours' comment for the worked examples), and auto-approve-
within-buffer with the comparison inverted from the pre-existing
(now-replaced) logic that auto-approved once overtime reached *at
least* a threshold.

Both methods only ever read attendance_rule_set_snapshot (a plain dict)
and mutate `self` -- no DB round trip needed, so most of these are
exercised directly on in-memory (unsaved) Attendance instances.
"""

from datetime import date, datetime, time

from django.test import TestCase
from django.utils import timezone

from attendance.models import Attendance, AttendanceRuleSet
from attendance.views.clock_in_out import (
    clock_in_attendance_and_activity,
    clock_out_attendance_and_activity,
)
from base.models import EmployeeShift, EmployeeShiftDay, EmployeeShiftSchedule
from horilla.testkit.factories import make_company, make_employee, make_user


def _snapshot(**own_overrides):
    own = {name: None for name in AttendanceRuleSet.RULE_FIELDS}
    own.update(own_overrides)
    return {"own": own, "company_default": None}


class UpdateAttendanceOvertimeTests(TestCase):
    def setUp(self):
        # get_shift_end_time() reads a real EmployeeShiftSchedule row --
        # only needed by the Shift-based tests below, but cheap enough
        # to always have available. Shift 10-6, matching the worked
        # example in AttendanceRuleSet.ot_threshold_hours' comment.
        self.shift = EmployeeShift.objects.create(employee_shift="Day Shift")
        self.day = EmployeeShiftDay.objects.create(day="monday")
        self.schedule = EmployeeShiftSchedule.objects.create(
            day=self.day, shift_id=self.shift,
            minimum_working_hour="08:00",
            start_time=time(10, 0), end_time=time(18, 0),
        )

    def _attendance(self, with_shift=False, **kwargs):
        defaults = dict(
            attendance_date=date(2026, 9, 21),
            attendance_worked_hour="09:30",
            minimum_hour="08:00",
            attendance_clock_out=None,
            attendance_clock_out_date=None,
        )
        if with_shift:
            defaults["shift_id"] = self.shift
            defaults["attendance_day"] = self.day
        defaults.update(kwargs)
        return Attendance(**defaults)

    def test_track_overtime_off_gives_zero_overtime(self):
        attendance = self._attendance(
            attendance_rule_set_snapshot=_snapshot(
                mode=AttendanceRuleSet.MODE_SHIFT_BASED, track_overtime=False,
            )
        )
        attendance.update_attendance_overtime()
        self.assertEqual(attendance.attendance_overtime, "00:00")
        self.assertEqual(attendance.overtime_second, 0)

    def test_no_snapshot_at_all_gives_zero_overtime(self):
        attendance = self._attendance(attendance_rule_set_snapshot=None)
        attendance.update_attendance_overtime()
        self.assertEqual(attendance.overtime_second, 0)

    def test_flexible_mode_uses_baseline_plus_threshold(self):
        # baseline 8h + threshold 1:30 -> overtime starts after 9:30
        # worked. 11:00 worked -> 1:30 overtime.
        attendance = self._attendance(
            attendance_worked_hour="11:00",
            attendance_rule_set_snapshot=_snapshot(
                mode=AttendanceRuleSet.MODE_FLEXIBLE, track_overtime=True,
                total_work_hours_reference="8.00", ot_threshold_hours="1.50",
            ),
        )
        attendance.update_attendance_overtime()
        self.assertEqual(attendance.attendance_overtime, "01:30")

    def test_flexible_mode_under_effective_threshold_gives_zero(self):
        attendance = self._attendance(
            attendance_worked_hour="09:00",  # under 9:30
            attendance_rule_set_snapshot=_snapshot(
                mode=AttendanceRuleSet.MODE_FLEXIBLE, track_overtime=True,
                total_work_hours_reference="8.00", ot_threshold_hours="1.50",
            ),
        )
        attendance.update_attendance_overtime()
        self.assertEqual(attendance.attendance_overtime, "00:00")

    def test_flexible_mode_without_baseline_falls_back_to_minimum_hour(self):
        attendance = self._attendance(
            attendance_rule_set_snapshot=_snapshot(
                mode=AttendanceRuleSet.MODE_FLEXIBLE, track_overtime=True,
                ot_threshold_hours="1.50",
                # total_work_hours_reference left blank
            )
        )
        attendance.update_attendance_overtime()
        # 09:30 worked - 08:00 minimum_hour = 01:30
        self.assertEqual(attendance.attendance_overtime, "01:30")

    def test_flexible_mode_without_threshold_falls_back_to_minimum_hour(self):
        attendance = self._attendance(
            attendance_rule_set_snapshot=_snapshot(
                mode=AttendanceRuleSet.MODE_FLEXIBLE, track_overtime=True,
                total_work_hours_reference="8.00",
                # ot_threshold_hours left blank
            )
        )
        attendance.update_attendance_overtime()
        self.assertEqual(attendance.attendance_overtime, "01:30")

    def test_shift_based_mode_uses_shift_end_plus_threshold(self):
        # shift ends 18:00 + threshold 1:30 -> overtime starts 19:30.
        attendance = self._attendance(
            with_shift=True,
            attendance_clock_out=time(19, 45),
            attendance_clock_out_date=date(2026, 9, 21),
            attendance_rule_set_snapshot=_snapshot(
                mode=AttendanceRuleSet.MODE_SHIFT_BASED, track_overtime=True,
                ot_threshold_hours="1.50",
            ),
        )
        attendance.update_attendance_overtime()
        self.assertEqual(attendance.attendance_overtime, "00:15")

    def test_shift_based_mode_clocked_out_before_ot_start_gives_zero(self):
        attendance = self._attendance(
            with_shift=True,
            attendance_clock_out=time(19, 0),  # before 19:30
            attendance_clock_out_date=date(2026, 9, 21),
            attendance_rule_set_snapshot=_snapshot(
                mode=AttendanceRuleSet.MODE_SHIFT_BASED, track_overtime=True,
                ot_threshold_hours="1.50",
            ),
        )
        attendance.update_attendance_overtime()
        self.assertEqual(attendance.attendance_overtime, "00:00")

    def test_shift_based_mode_not_yet_clocked_out_falls_back(self):
        attendance = self._attendance(
            with_shift=True,
            attendance_clock_out=None,
            attendance_rule_set_snapshot=_snapshot(
                mode=AttendanceRuleSet.MODE_SHIFT_BASED, track_overtime=True,
                ot_threshold_hours="1.50",
            ),
        )
        attendance.update_attendance_overtime()
        # Nothing to compare against yet -- falls back to
        # worked-hour-vs-minimum_hour: 09:30 - 08:00 = 01:30.
        self.assertEqual(attendance.attendance_overtime, "01:30")

    def test_shift_based_mode_without_threshold_falls_back(self):
        attendance = self._attendance(
            with_shift=True,
            attendance_clock_out=time(19, 45),
            attendance_clock_out_date=date(2026, 9, 21),
            attendance_rule_set_snapshot=_snapshot(
                mode=AttendanceRuleSet.MODE_SHIFT_BASED, track_overtime=True,
                # ot_threshold_hours left blank
            ),
        )
        attendance.update_attendance_overtime()
        self.assertEqual(attendance.attendance_overtime, "01:30")

    def test_shift_based_mode_with_no_shift_at_all_falls_back(self):
        # with_shift=False -- get_shift_end_time() has nothing to look
        # up (attendance_day is None), same as no threshold configured.
        attendance = self._attendance(
            attendance_clock_out=time(19, 45),
            attendance_clock_out_date=date(2026, 9, 21),
            attendance_rule_set_snapshot=_snapshot(
                mode=AttendanceRuleSet.MODE_SHIFT_BASED, track_overtime=True,
                ot_threshold_hours="1.50",
            ),
        )
        attendance.update_attendance_overtime()
        self.assertEqual(attendance.attendance_overtime, "01:30")

    def test_clock_out_still_a_raw_string_is_parsed(self):
        # clock_out_attendance_and_activity() assigns attendance_clock_out
        # as a raw "HH:MM:SS" string moments before calling save() --
        # Django's TimeField only coerces it to a real time object on the
        # way to the database, not on plain attribute assignment.
        attendance = self._attendance(
            with_shift=True,
            attendance_clock_out="19:45:00",
            attendance_clock_out_date=date(2026, 9, 21),
            attendance_rule_set_snapshot=_snapshot(
                mode=AttendanceRuleSet.MODE_SHIFT_BASED, track_overtime=True,
                ot_threshold_hours="1.50",
            ),
        )
        attendance.update_attendance_overtime()
        self.assertEqual(attendance.attendance_overtime, "00:15")


class HandleOvertimeConditionsTests(TestCase):
    def _attendance(self, overtime_second, **snapshot_overrides):
        attendance = Attendance(
            attendance_date=date(2026, 9, 21),
            overtime_second=overtime_second,
            attendance_rule_set_snapshot=_snapshot(**snapshot_overrides),
        )
        return attendance

    def test_track_overtime_off_never_auto_approves(self):
        attendance = self._attendance(
            3600, mode=AttendanceRuleSet.MODE_SHIFT_BASED, track_overtime=False,
        )
        attendance.handle_overtime_conditions()
        self.assertFalse(attendance.attendance_overtime_approve)

    def test_shift_based_within_buffer_auto_approves(self):
        attendance = self._attendance(
            20 * 60,  # 20 minutes of overtime
            mode=AttendanceRuleSet.MODE_SHIFT_BASED, track_overtime=True,
            shift_ot_auto_approve_buffer_minutes=30,
        )
        attendance.handle_overtime_conditions()
        self.assertTrue(attendance.attendance_overtime_approve)

    def test_shift_based_beyond_buffer_is_not_auto_approved(self):
        attendance = self._attendance(
            45 * 60,  # 45 minutes of overtime
            mode=AttendanceRuleSet.MODE_SHIFT_BASED, track_overtime=True,
            shift_ot_auto_approve_buffer_minutes=30,
        )
        attendance.handle_overtime_conditions()
        self.assertFalse(attendance.attendance_overtime_approve)

    def test_flexible_within_buffer_auto_approves(self):
        attendance = self._attendance(
            int(0.5 * 3600),  # 30 minutes
            mode=AttendanceRuleSet.MODE_FLEXIBLE, track_overtime=True,
            flexible_ot_auto_approve_buffer_hours="1.00",
        )
        attendance.handle_overtime_conditions()
        self.assertTrue(attendance.attendance_overtime_approve)

    def test_flexible_beyond_buffer_is_not_auto_approved(self):
        attendance = self._attendance(
            int(2 * 3600),  # 2 hours
            mode=AttendanceRuleSet.MODE_FLEXIBLE, track_overtime=True,
            flexible_ot_auto_approve_buffer_hours="1.00",
        )
        attendance.handle_overtime_conditions()
        self.assertFalse(attendance.attendance_overtime_approve)

    def test_no_buffer_configured_means_any_overtime_needs_manual_approval(self):
        attendance = self._attendance(
            1, mode=AttendanceRuleSet.MODE_SHIFT_BASED, track_overtime=True,
        )
        attendance.handle_overtime_conditions()
        self.assertFalse(attendance.attendance_overtime_approve)

    def test_zero_overtime_is_never_auto_approved(self):
        """
        Regression: this runs on every save, including the very first
        one at clock-in, when there's genuinely no overtime yet (0).
        Auto-approving on that trivial 0 would permanently "stick" a
        false-positive approval, since this method never resets the
        flag back to False afterward -- see the method's own comment.
        """
        attendance = self._attendance(
            0, mode=AttendanceRuleSet.MODE_SHIFT_BASED, track_overtime=True,
            shift_ot_auto_approve_buffer_minutes=30,
        )
        attendance.handle_overtime_conditions()
        self.assertFalse(attendance.attendance_overtime_approve)

    def test_never_resets_an_already_approved_record_to_false(self):
        attendance = self._attendance(
            999 * 60,  # a large overtime, well beyond any buffer
            mode=AttendanceRuleSet.MODE_SHIFT_BASED, track_overtime=True,
            shift_ot_auto_approve_buffer_minutes=30,
        )
        attendance.attendance_overtime_approve = True  # a prior manual approval
        attendance.handle_overtime_conditions()
        self.assertTrue(attendance.attendance_overtime_approve)


class OvertimeEndToEndClockOutTests(TestCase):
    """
    Drives the real clock-in/clock-out pipeline (clock_in_attendance_and_
    activity() -> clock_out_attendance_and_activity() -> Attendance.save())
    to confirm the whole chain composes correctly: rule-set snapshot
    captured at clock-in, read back by update_attendance_overtime()/
    handle_overtime_conditions() inside save() at clock-out.
    """

    def setUp(self):
        self.company = make_company("Acme")
        self.shift = EmployeeShift.objects.create(employee_shift="Day Shift")
        self.shift.company_id.add(self.company)
        self.day = EmployeeShiftDay.objects.create(day="monday")
        EmployeeShiftSchedule.objects.create(
            day=self.day, shift_id=self.shift,
            minimum_working_hour="08:00",
            start_time=time(10, 0), end_time=time(18, 0),
        )
        self.employee = make_employee(
            company=self.company, email="ot1@test.horilla",
            user=make_user("ot1"), shift=self.shift,
        )
        AttendanceRuleSet.objects.create(
            tier="COMPANY", company=self.company,
            mode=AttendanceRuleSet.MODE_SHIFT_BASED,
            track_overtime=True,
            ot_threshold_hours="1.50",  # shift ends 18:00 -> OT starts 19:30
            shift_ot_auto_approve_buffer_minutes=30,
        )

    def _clock_in(self):
        return clock_in_attendance_and_activity(
            employee=self.employee,
            date_today=date(2026, 9, 21),  # a Monday
            attendance_date=date(2026, 9, 21),
            day=self.day,
            now="10:00",
            shift=self.shift,
            minimum_hour="08:00",
            start_time=36000,
            end_time=64800,
            in_datetime=timezone.make_aware(datetime(2026, 9, 21, 10, 0)),
        )

    def _clock_out(self, out_time):
        return clock_out_attendance_and_activity(
            employee=self.employee,
            date_today=date(2026, 9, 21),
            now=out_time.strftime("%H:%M"),
            out_datetime=timezone.make_aware(
                datetime(2026, 9, 21, out_time.hour, out_time.minute)
            ),
        )

    def test_overtime_within_buffer_is_auto_approved(self):
        self._clock_in()
        attendance = self._clock_out(time(19, 50))  # 20 min past OT start (19:30)
        self.assertEqual(attendance.overtime_second, 20 * 60)
        self.assertTrue(attendance.attendance_overtime_approve)

    def test_overtime_beyond_buffer_needs_manual_approval(self):
        self._clock_in()
        attendance = self._clock_out(time(20, 15))  # 45 min past OT start (19:30)
        self.assertEqual(attendance.overtime_second, 45 * 60)
        self.assertFalse(attendance.attendance_overtime_approve)


class ValidationAndOvertimeTogetherTests(TestCase):
    """
    Validation (#6) is no longer an independent check -- attendance_validate()
    (attendance/views/views.py) now derives it entirely from the same
    overtime auto-approve buffer decision Overtime (#7) makes for
    attendance_overtime_approve (see validation_threshold's removal,
    attendance/models.py). Confirm that coupling holds on a real
    clock-out, not just attendance_validate() in isolation (see
    test_attendance_validate.py for that).
    """

    def setUp(self):
        self.company = make_company("Acme")
        self.shift = EmployeeShift.objects.create(employee_shift="Day Shift")
        self.shift.company_id.add(self.company)
        self.day = EmployeeShiftDay.objects.create(day="monday")
        EmployeeShiftSchedule.objects.create(
            day=self.day, shift_id=self.shift,
            minimum_working_hour="08:00",
            start_time=time(10, 0), end_time=time(18, 0),
        )
        self.employee = make_employee(
            company=self.company, email="combo1@test.horilla",
            user=make_user("combo1"), shift=self.shift,
        )
        AttendanceRuleSet.objects.create(
            tier="COMPANY", company=self.company,
            mode=AttendanceRuleSet.MODE_SHIFT_BASED,
            track_overtime=True,
            ot_threshold_hours="1.50",  # shift ends 18:00 -> OT starts 19:30
            shift_ot_auto_approve_buffer_minutes=30,
        )

    def _clock_in(self):
        return clock_in_attendance_and_activity(
            employee=self.employee,
            date_today=date(2026, 9, 21),  # a Monday
            attendance_date=date(2026, 9, 21),
            day=self.day,
            now="10:00",
            shift=self.shift,
            minimum_hour="08:00",
            start_time=36000,
            end_time=64800,
            in_datetime=timezone.make_aware(datetime(2026, 9, 21, 10, 0)),
        )

    def _clock_out(self, out_time):
        return clock_out_attendance_and_activity(
            employee=self.employee,
            date_today=date(2026, 9, 21),
            now=out_time.strftime("%H:%M"),
            out_datetime=timezone.make_aware(
                datetime(2026, 9, 21, out_time.hour, out_time.minute)
            ),
        )

    def test_short_day_with_no_overtime_auto_validates(self):
        self._clock_in()
        attendance = self._clock_out(time(18, 0))  # 8:00 worked, no OT yet
        self.assertEqual(attendance.attendance_worked_hour, "08:00")
        self.assertEqual(attendance.overtime_second, 0)  # before 19:30 OT start
        self.assertFalse(attendance.attendance_overtime_approve)  # nothing to approve
        self.assertTrue(attendance.attendance_validated)  # no overtime -> trivially validates

    def test_overtime_within_buffer_auto_validates_the_whole_record(self):
        """
        The case worth proving under the new coupling: once overtime
        exists, the record's own validation follows the *same* decision
        as the overtime's own auto-approval, not a separate worked-hours
        check -- 20 min of overtime here, within the 30-min buffer.
        """
        self._clock_in()
        attendance = self._clock_out(time(19, 50))  # 9:50 worked
        self.assertEqual(attendance.attendance_worked_hour, "09:50")
        self.assertEqual(attendance.overtime_second, 20 * 60)  # 20 min past 19:30
        self.assertTrue(attendance.attendance_overtime_approve)  # within 30-min buffer
        self.assertTrue(attendance.attendance_validated)

    def test_overtime_beyond_buffer_needs_review_on_both(self):
        self._clock_in()
        attendance = self._clock_out(time(20, 15))  # 45 min past OT start (19:30)
        self.assertEqual(attendance.overtime_second, 45 * 60)
        self.assertFalse(attendance.attendance_overtime_approve)  # past 30-min buffer
        self.assertFalse(attendance.attendance_validated)


class ManualOvertimeApprovalTests(TestCase):
    """
    Auto-approve-within-buffer is one path to attendance_overtime_approve
    =True; the pre-existing manual approval endpoints (attendance/views/
    views.py::approve_overtime()/approve_bulk_overtime()) are the other,
    for exactly the overtime-beyond-the-buffer case auto-approve
    deliberately leaves alone. Both just do `attendance.
    attendance_overtime_approve = True; attendance.save()` on an
    already-loaded instance -- confirm that still survives
    update_attendance_overtime()/handle_overtime_conditions() running
    again inside that save(), and that the ledger credits correctly.
    """

    def setUp(self):
        self.company = make_company("Acme")
        self.shift = EmployeeShift.objects.create(employee_shift="Day Shift")
        self.shift.company_id.add(self.company)
        self.day = EmployeeShiftDay.objects.create(day="monday")
        EmployeeShiftSchedule.objects.create(
            day=self.day, shift_id=self.shift,
            minimum_working_hour="08:00",
            start_time=time(10, 0), end_time=time(18, 0),
        )
        self.employee = make_employee(
            company=self.company, email="manual1@test.horilla",
            user=make_user("manual1"), shift=self.shift,
        )
        AttendanceRuleSet.objects.create(
            tier="COMPANY", company=self.company,
            mode=AttendanceRuleSet.MODE_SHIFT_BASED,
            track_overtime=True,
            ot_threshold_hours="1.50",  # shift ends 18:00 -> OT starts 19:30
            shift_ot_auto_approve_buffer_minutes=30,
        )
        clock_in_attendance_and_activity(
            employee=self.employee,
            date_today=date(2026, 9, 21),  # a Monday
            attendance_date=date(2026, 9, 21),
            day=self.day,
            now="10:00",
            shift=self.shift,
            minimum_hour="08:00",
            start_time=36000,
            end_time=64800,
            in_datetime=timezone.make_aware(datetime(2026, 9, 21, 10, 0)),
        )
        self.attendance = clock_out_attendance_and_activity(
            employee=self.employee,
            date_today=date(2026, 9, 21),
            now="20:15",
            out_datetime=timezone.make_aware(datetime(2026, 9, 21, 20, 15)),
        )

    def test_beyond_buffer_overtime_is_not_auto_approved(self):
        # Sanity check on the fixture: 45 min past 19:30 OT start,
        # beyond the 30-min buffer -- confirms there's something to
        # manually approve in the first place.
        self.assertEqual(self.attendance.overtime_second, 45 * 60)
        self.assertFalse(self.attendance.attendance_overtime_approve)

    def test_manual_approval_survives_the_save_pipeline_and_credits_the_ledger(self):
        from attendance.models import AttendanceOverTime

        self.attendance.attendance_overtime_approve = True
        self.attendance.save()

        self.attendance.refresh_from_db()
        self.assertTrue(self.attendance.attendance_overtime_approve)
        self.assertEqual(self.attendance.approved_overtime_second, 45 * 60)

        ledger = AttendanceOverTime.objects.get(
            employee_id=self.employee, month="september", year=2026,
        )
        self.assertEqual(ledger.overtime_second, 45 * 60)
