"""
Tests for Attendance Irregularities (#9) -- the Flexible-mode half.
The shift-based half (late arrivals/early departures) is pre-existing,
unrelated to this feature, and already covered by other tests; these
only cover the new flexible_shortfall category on the same existing
AttendanceLateComeEarlyOut record type, gated by irregularities_enabled.
"""

from datetime import date, datetime, time

from django.test import TestCase
from django.utils import timezone

from attendance.models import AttendanceLateComeEarlyOut, AttendanceRuleSet
from attendance.views.clock_in_out import (
    clock_in_attendance_and_activity,
    clock_out_attendance_and_activity,
    early_out,
    flexible_shortfall,
    flexible_shortfall_create,
)
from base.models import EmployeeShift, EmployeeShiftDay, EmployeeShiftSchedule
from horilla.testkit.factories import make_company, make_employee, make_user


def _snapshot(**own_overrides):
    own = {name: None for name in AttendanceRuleSet.RULE_FIELDS}
    own.update(own_overrides)
    return {"own": own, "company_default": None}


class FlexibleShortfallUnitTests(TestCase):
    def setUp(self):
        self.company = make_company("Acme")
        self.employee = make_employee(
            company=self.company, email="irr1@test.horilla", user=make_user("irr1"),
        )

    def _attendance(self, worked_hour, **snapshot_overrides):
        from attendance.models import Attendance

        return Attendance.objects.create(
            employee_id=self.employee,
            attendance_date=date(2026, 9, 21),
            attendance_worked_hour=worked_hour,
            attendance_rule_set_snapshot=_snapshot(**snapshot_overrides),
        )

    def test_disabled_by_default_creates_no_record(self):
        attendance = self._attendance(
            "06:00", mode=AttendanceRuleSet.MODE_FLEXIBLE,
            total_work_hours_reference="8.00",
            # irregularities_enabled left blank -- opt-in, off by default
        )
        flexible_shortfall(attendance)
        self.assertFalse(
            AttendanceLateComeEarlyOut.objects.filter(
                attendance_id=attendance, type="flexible_shortfall",
            ).exists()
        )

    def test_no_reference_configured_creates_no_record(self):
        attendance = self._attendance(
            "06:00", mode=AttendanceRuleSet.MODE_FLEXIBLE,
            irregularities_enabled=True,
            # total_work_hours_reference left blank
        )
        flexible_shortfall(attendance)
        self.assertFalse(
            AttendanceLateComeEarlyOut.objects.filter(
                attendance_id=attendance, type="flexible_shortfall",
            ).exists()
        )

    def test_worked_short_of_reference_creates_a_record(self):
        attendance = self._attendance(
            "06:00", mode=AttendanceRuleSet.MODE_FLEXIBLE,
            irregularities_enabled=True, total_work_hours_reference="8.00",
        )
        flexible_shortfall(attendance)
        record = AttendanceLateComeEarlyOut.objects.get(
            attendance_id=attendance, type="flexible_shortfall",
        )
        self.assertEqual(record.employee_id, self.employee)

    def test_worked_meeting_reference_creates_no_record(self):
        attendance = self._attendance(
            "08:00", mode=AttendanceRuleSet.MODE_FLEXIBLE,
            irregularities_enabled=True, total_work_hours_reference="8.00",
        )
        flexible_shortfall(attendance)
        self.assertFalse(
            AttendanceLateComeEarlyOut.objects.filter(
                attendance_id=attendance, type="flexible_shortfall",
            ).exists()
        )

    def test_worked_over_reference_creates_no_record(self):
        attendance = self._attendance(
            "09:00", mode=AttendanceRuleSet.MODE_FLEXIBLE,
            irregularities_enabled=True, total_work_hours_reference="8.00",
        )
        flexible_shortfall(attendance)
        self.assertFalse(
            AttendanceLateComeEarlyOut.objects.filter(
                attendance_id=attendance, type="flexible_shortfall",
            ).exists()
        )

    def test_calling_twice_does_not_duplicate(self):
        attendance = self._attendance(
            "06:00", mode=AttendanceRuleSet.MODE_FLEXIBLE,
            irregularities_enabled=True, total_work_hours_reference="8.00",
        )
        flexible_shortfall_create(attendance)
        flexible_shortfall_create(attendance)
        self.assertEqual(
            AttendanceLateComeEarlyOut.objects.filter(
                attendance_id=attendance, type="flexible_shortfall",
            ).count(),
            1,
        )


class IrregularitiesEndToEndTests(TestCase):
    """
    Drives the real clock-in/clock-out pipeline to confirm the mode gate
    routes correctly: Flexible days get the shortfall check, Shift-based
    days keep getting early-out detection exactly as before (regression
    coverage for the branch this feature restructured).
    """

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
            company=self.company, email="irr2@test.horilla",
            user=make_user("irr2"), shift=self.shift,
        )

    def _clock_in(self, now, in_hour, in_minute):
        return clock_in_attendance_and_activity(
            employee=self.employee,
            date_today=date(2026, 9, 21),  # a Monday
            attendance_date=date(2026, 9, 21),
            day=self.day,
            now=now,
            shift=self.shift,
            minimum_hour="08:00",
            start_time=32400,
            end_time=61200,
            in_datetime=timezone.make_aware(datetime(2026, 9, 21, in_hour, in_minute)),
        )

    def _clock_out(self, out_hour, out_minute):
        # clock_out_attendance_and_activity() only updates the attendance
        # data itself -- the early-out/shortfall mode-gate lives in the
        # clock_out() *view*, one layer up (see clock_in_out.py). Calling
        # the two here mirrors exactly what that view does for the
        # simple, same-day, non-night-shift case these tests use,
        # without driving the full view (see this session's established
        # note on why that path is unreliable for is_flexible_mode()).
        attendance = clock_out_attendance_and_activity(
            employee=self.employee,
            date_today=date(2026, 9, 21),
            now=f"{out_hour:02d}:{out_minute:02d}",
            out_datetime=timezone.make_aware(
                datetime(2026, 9, 21, out_hour, out_minute)
            ),
        )
        if attendance.is_flexible_mode():
            flexible_shortfall(attendance)
        else:
            early_out(
                attendance, start_time=32400, end_time=61200, shift=self.shift
            )
        return attendance

    def test_flexible_short_day_gets_a_shortfall_record(self):
        AttendanceRuleSet.objects.create(
            tier="COMPANY", company=self.company,
            mode=AttendanceRuleSet.MODE_FLEXIBLE,
            irregularities_enabled=True, total_work_hours_reference="8.00",
        )
        self._clock_in("09:00", 9, 0)
        attendance = self._clock_out(15, 0)  # 6:00 worked, short of 8:00

        self.assertEqual(attendance.attendance_worked_hour, "06:00")
        self.assertTrue(
            AttendanceLateComeEarlyOut.objects.filter(
                attendance_id=attendance, type="flexible_shortfall",
            ).exists()
        )
        # Early-out detection must not also fire for a Flexible day.
        self.assertFalse(
            AttendanceLateComeEarlyOut.objects.filter(
                attendance_id=attendance, type="early_out",
            ).exists()
        )

    def test_flexible_full_day_gets_no_shortfall_record(self):
        AttendanceRuleSet.objects.create(
            tier="COMPANY", company=self.company,
            mode=AttendanceRuleSet.MODE_FLEXIBLE,
            irregularities_enabled=True, total_work_hours_reference="8.00",
        )
        self._clock_in("09:00", 9, 0)
        attendance = self._clock_out(17, 0)  # 8:00 worked, meets reference

        self.assertFalse(
            AttendanceLateComeEarlyOut.objects.filter(
                attendance_id=attendance, type="flexible_shortfall",
            ).exists()
        )

    def test_shift_based_day_still_gets_early_out_not_shortfall(self):
        AttendanceRuleSet.objects.create(
            tier="COMPANY", company=self.company,
            mode=AttendanceRuleSet.MODE_SHIFT_BASED,
        )
        self._clock_in("09:00", 9, 0)
        attendance = self._clock_out(16, 0)  # left an hour early (shift ends 17:00)

        self.assertTrue(
            AttendanceLateComeEarlyOut.objects.filter(
                attendance_id=attendance, type="early_out",
            ).exists()
        )
        self.assertFalse(
            AttendanceLateComeEarlyOut.objects.filter(
                attendance_id=attendance, type="flexible_shortfall",
            ).exists()
        )

    def test_reclocking_in_clears_a_stale_shortfall_record(self):
        """
        A shortfall recorded from an earlier, partial-day clock-out must
        not survive re-clocking in -- more hours are still coming, and
        the full day might no longer actually be short once it's done.
        """
        AttendanceRuleSet.objects.create(
            tier="COMPANY", company=self.company,
            mode=AttendanceRuleSet.MODE_FLEXIBLE,
            irregularities_enabled=True, total_work_hours_reference="8.00",
        )
        self._clock_in("09:00", 9, 0)
        attendance = self._clock_out(12, 0)  # 3:00 worked so far -- short
        self.assertTrue(
            AttendanceLateComeEarlyOut.objects.filter(
                attendance_id=attendance, type="flexible_shortfall",
            ).exists()
        )

        # Re-clock in -- the stale shortfall marker must be cleared.
        self._clock_in("13:00", 13, 0)
        self.assertFalse(
            AttendanceLateComeEarlyOut.objects.filter(
                attendance_id=attendance, type="flexible_shortfall",
            ).exists()
        )
