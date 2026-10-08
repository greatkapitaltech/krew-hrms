"""
Web-view tests for the new manager-initiated Overtime approval queue
(attendance/cbv/overtime_approval.py) -- confirmed via earlier research
that nothing like this existed: handle_overtime_conditions() only ever
sets attendance_overtime_approve True itself, and the only existing
screen touching overtime (HourAccount) operates on the monthly
AttendanceOverTime total, not a per-day decision.
"""

from datetime import date, timedelta

from django.test import TestCase

from attendance.models import Attendance, AttendanceActivityLog, AttendanceRuleSet
from employee.models import EmployeeWorkInformation
from horilla.horilla_middlewares import _thread_locals
from horilla.testkit.company import clear_selected_company
from horilla.testkit.factories import make_company, make_employee, make_user


class OvertimeApprovalTestBase(TestCase):
    PAGE_URL = "/attendance/overtime-approval/"
    LIST_URL = "/attendance/overtime-approval/list/"
    HX = {"HTTP_HX_REQUEST": "true"}

    def setUp(self):
        _thread_locals.request = None
        clear_selected_company()
        self.addCleanup(self._clear_context_vars)

        self.company = make_company("Overtime Approval Co")
        self.manager_user = make_user("ot_manager")
        self.manager = make_employee(
            company=self.company, email="ot_manager@test.horilla", user=self.manager_user,
        )
        self.employee = make_employee(
            company=self.company, email="ot_employee@test.horilla", user=make_user("ot_employee"),
        )
        EmployeeWorkInformation.objects.filter(employee_id=self.employee).update(
            reporting_manager_id=self.manager
        )
        self.client.force_login(self.manager_user)

        # overtime_second is computed by Attendance.save() from the
        # resolved AttendanceRuleSet snapshot, not settable directly --
        # a Flexible-mode rule with track_overtime on and no auto-
        # approve buffer (0 tolerance) is what keeps the resulting
        # overtime both nonzero and pending, same fixture shape as
        # test_overtime_calculation.py's own tests.
        rule_set = AttendanceRuleSet.objects.create(
            tier="COMPANY", company=self.company, mode=AttendanceRuleSet.MODE_FLEXIBLE,
            track_overtime=True, total_work_hours_reference="8.00", ot_threshold_minutes=60,
        )
        day = date.today() - timedelta(days=1)
        self.attendance = Attendance.objects.create(
            employee_id=self.employee, attendance_date=day,
            attendance_clock_in="09:00", attendance_clock_in_date=day,
            attendance_clock_out="21:00", attendance_clock_out_date=day,
            # Nothing in the model derives this from clock_in/clock_out --
            # every real caller (clock_out_attendance_and_activity(),
            # create_attendance.py) computes and sets it explicitly
            # before save(); update_attendance_overtime() just reads it
            # back out (self.at_work_second = strtime_seconds(self.
            # attendance_worked_hour)), confirmed directly after this
            # came back zero despite a correctly configured rule_set.
            attendance_worked_hour="12:00",
            attendance_rule_set=rule_set,
            attendance_rule_set_snapshot=AttendanceRuleSet.capture_snapshot(rule_set),
        )
        self.attendance.refresh_from_db()

    def _clear_context_vars(self):
        _thread_locals.request = None
        clear_selected_company()


class QueueTests(OvertimeApprovalTestBase):
    def test_page_and_list_render(self):
        self.assertEqual(self.client.get(self.PAGE_URL).status_code, 200)
        response = self.client.get(self.LIST_URL, **self.HX)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, str(self.employee))

    def test_approve_sets_both_fields_and_logs(self):
        response = self.client.post(f"/attendance/overtime-approval/{self.attendance.pk}/approve/")
        self.assertEqual(response.status_code, 200)
        self.attendance.refresh_from_db()
        self.assertTrue(self.attendance.attendance_overtime_approve)
        self.assertEqual(self.attendance.overtime_decision, Attendance.OVERTIME_DECISION_APPROVED)
        self.assertTrue(
            AttendanceActivityLog.objects.filter(
                action_type=AttendanceActivityLog.ACTION_OVERTIME_MANUAL_DECISION,
            ).exists()
        )

    def test_reject_without_remark_is_blocked(self):
        response = self.client.post(f"/attendance/overtime-approval/{self.attendance.pk}/reject/")
        self.assertEqual(response.status_code, 200)
        self.attendance.refresh_from_db()
        self.assertEqual(self.attendance.overtime_decision, Attendance.OVERTIME_DECISION_PENDING)

    def test_reject_with_remark_denies(self):
        response = self.client.post(
            f"/attendance/overtime-approval/{self.attendance.pk}/reject/",
            {"resolution_note": "Not pre-approved"},
        )
        self.assertEqual(response.status_code, 200)
        self.attendance.refresh_from_db()
        self.assertEqual(self.attendance.overtime_decision, Attendance.OVERTIME_DECISION_DENIED)
        self.assertFalse(self.attendance.attendance_overtime_approve)

    def test_decided_row_drops_out_of_the_queue(self):
        self.client.post(f"/attendance/overtime-approval/{self.attendance.pk}/approve/")
        response = self.client.get(self.LIST_URL, **self.HX)
        self.assertNotContains(response, str(self.employee))

    def test_a_locked_payroll_period_blocks_approve_and_reject(self):
        from attendance.models import PayrollReadinessSnapshot

        PayrollReadinessSnapshot.objects.create(
            company=self.company,
            start_date=self.attendance.attendance_date,
            end_date=self.attendance.attendance_date,
            locked_by=self.manager,
        )
        self.client.post(f"/attendance/overtime-approval/{self.attendance.pk}/approve/")
        self.attendance.refresh_from_db()
        self.assertEqual(self.attendance.overtime_decision, Attendance.OVERTIME_DECISION_PENDING)

        self.client.post(
            f"/attendance/overtime-approval/{self.attendance.pk}/reject/",
            {"resolution_note": "Doesn't matter"},
        )
        self.attendance.refresh_from_db()
        self.assertEqual(self.attendance.overtime_decision, Attendance.OVERTIME_DECISION_PENDING)

    def test_someone_outside_the_team_cannot_decide(self):
        outside_user = make_user("ot_outsider")
        make_employee(company=self.company, email="ot_outsider@test.horilla", user=outside_user)
        self.client.force_login(outside_user)
        response = self.client.post(f"/attendance/overtime-approval/{self.attendance.pk}/approve/")
        self.assertEqual(response.status_code, 200)
        self.attendance.refresh_from_db()
        self.assertEqual(self.attendance.overtime_decision, Attendance.OVERTIME_DECISION_PENDING)
