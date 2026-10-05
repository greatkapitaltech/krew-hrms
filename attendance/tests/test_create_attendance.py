"""
Web-view tests for Create Attendance (attendance/cbv/create_attendance.py)
-- manual single-entry attendance creation that bypasses Validation and
Overtime approval entirely (both forced True directly, never evaluated,
per the PRD's explicit bypass), distinct from the older
is_validate_request-based manual flow which routes into a manager
queue instead.

Covers: Shift-based requires a shift, Flexible doesn't; a successful
create writes exactly one Attendance + one AttendanceActivity row with
creation_source=MANUAL; an existing record on that employee/date routes
to the override-confirmation step instead of saving immediately; the
confirmed override replaces the prior AttendanceActivity row(s); and
exactly one AttendanceActivityLog row (MANUAL_CREATE_OVERRIDE) is
written per create/override.
"""

from datetime import date, timedelta

from django.test import TestCase

from attendance.models import Attendance, AttendanceActivity, AttendanceActivityLog, AttendanceRuleSet
from base.models import Department, EmployeeShift
from horilla.horilla_middlewares import _thread_locals
from horilla.testkit.company import clear_selected_company
from horilla.testkit.factories import make_company, make_employee, make_user


class CreateAttendanceWebTestBase(TestCase):
    PAGE_URL = "/attendance/create-attendance/"
    LIST_URL = "/attendance/create-attendance/list/"
    NAV_URL = "/attendance/create-attendance/nav/"
    CREATE_URL = "/attendance/create-attendance/create/"

    HX = {"HTTP_HX_REQUEST": "true"}

    def setUp(self):
        _thread_locals.request = None
        clear_selected_company()
        self.addCleanup(self._clear_context_vars)

        self.company = make_company("Create Attendance Co")
        self.shift = EmployeeShift.objects.create(employee_shift="Day Shift")
        self.shift_department = Department.objects.create(department="Ops")
        self.shift_department.company_id.add(self.company)

        # Superuser, matching the precedent in test_attendance_rule_set_web.py
        # -- has_perm() short-circuits True for a superuser regardless of
        # which AUTHENTICATION_BACKENDS chain is active, so this exercises
        # the view without depending on how can_create_attendance is granted.
        self.admin_user = make_user("create_att_admin", is_superuser=True)
        self.admin = make_employee(
            company=self.company,
            email="create_att_admin@test.horilla",
            user=self.admin_user,
        )
        self.client.force_login(self.admin_user)

        # Company Default resolves to Flexible; the Department override
        # below is what makes shift_employee resolve to Shift-based instead
        # -- AttendanceRuleSet only allows one COMPANY-tier row per company,
        # so the two modes under test have to come from different tiers.
        AttendanceRuleSet.objects.create(
            tier="COMPANY", company=self.company, mode=AttendanceRuleSet.MODE_FLEXIBLE,
        )
        AttendanceRuleSet.objects.create(
            tier="DEPARTMENT",
            company=self.company,
            department=self.shift_department,
            mode=AttendanceRuleSet.MODE_SHIFT_BASED,
        )

        self.shift_employee = make_employee(
            company=self.company,
            email="shift_employee@test.horilla",
            user=make_user("shift_employee"),
            shift=self.shift,
            department=self.shift_department,
        )
        self.flexible_employee = make_employee(
            company=self.company,
            email="flexible_employee@test.horilla",
            user=make_user("flexible_employee"),
        )

    def _clear_context_vars(self):
        _thread_locals.request = None
        clear_selected_company()

    def _post_create(self, employee, attendance_date, **overrides):
        payload = {
            "employee_id": employee.pk,
            "attendance_clock_in": "09:00",
            "attendance_clock_in_date": attendance_date.isoformat(),
            "attendance_clock_out": "18:00",
            "attendance_clock_out_date": attendance_date.isoformat(),
            "reason": "Manual entry for testing",
        }
        payload.update(overrides)
        return self.client.post(self.CREATE_URL, payload, **self.HX)


class PageAccessTests(CreateAttendanceWebTestBase):
    ENTRY_URL = "/attendance/create-attendance/entry/"

    def test_page_list_and_nav_render(self):
        self.assertEqual(self.client.get(self.PAGE_URL).status_code, 200)
        self.assertEqual(self.client.get(self.LIST_URL, **self.HX).status_code, 200)
        self.assertEqual(self.client.get(self.NAV_URL, **self.HX).status_code, 200)

    def test_entry_screen_offers_all_three_modes(self):
        response = self.client.get(self.ENTRY_URL, **self.HX)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Single Entry")
        self.assertContains(response, "Multiple Employees, One Date")
        self.assertContains(response, "One Employee, Multiple Dates")

    def test_page_is_denied_without_the_dedicated_permission(self):
        other_user = make_user("no_perm_user")
        make_employee(
            company=self.company, email="no_perm@test.horilla", user=other_user,
        )
        self.client.force_login(other_user)
        response = self.client.get(self.PAGE_URL)
        self.assertNotEqual(response.status_code, 200)


class CreateTests(CreateAttendanceWebTestBase):
    def test_shift_based_employee_requires_a_shift(self):
        response = self._post_create(self.shift_employee, date.today() - timedelta(days=1))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            Attendance.objects.filter(employee_id=self.shift_employee).exists()
        )
        self.assertIn("shift_id", response.context["form"].errors)

    def test_shift_based_employee_creates_with_a_shift(self):
        attendance_date = date.today() - timedelta(days=1)
        response = self._post_create(
            self.shift_employee, attendance_date, shift_id=self.shift.pk,
        )
        self.assertEqual(response.status_code, 200)
        attendance = Attendance.objects.get(employee_id=self.shift_employee)
        self.assertEqual(attendance.attendance_date, attendance_date)
        self.assertEqual(attendance.creation_source, Attendance.CREATION_SOURCE_MANUAL)
        self.assertTrue(attendance.attendance_validated)
        self.assertEqual(
            AttendanceActivity.objects.filter(employee_id=self.shift_employee).count(), 1
        )
        log = AttendanceActivityLog.objects.get(
            action_type=AttendanceActivityLog.ACTION_MANUAL_CREATE_OVERRIDE
        )
        self.assertEqual(log.actor_id, self.admin.pk)
        self.assertEqual(log.affected_employee_ids, [self.shift_employee.pk])

    def test_flexible_employee_does_not_require_a_shift(self):
        attendance_date = date.today() - timedelta(days=1)
        response = self._post_create(self.flexible_employee, attendance_date)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            Attendance.objects.filter(employee_id=self.flexible_employee).exists()
        )

    def test_a_future_date_is_rejected(self):
        response = self._post_create(
            self.flexible_employee, date.today() + timedelta(days=1)
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            Attendance.objects.filter(employee_id=self.flexible_employee).exists()
        )
        self.assertIn("attendance_clock_in_date", response.context["form"].errors)

    def test_checkout_before_checkin_is_rejected(self):
        attendance_date = date.today() - timedelta(days=1)
        response = self._post_create(
            self.flexible_employee,
            attendance_date,
            attendance_clock_in="18:00",
            attendance_clock_out="09:00",
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            Attendance.objects.filter(employee_id=self.flexible_employee).exists()
        )


class OverrideTests(CreateAttendanceWebTestBase):
    def test_existing_record_prompts_for_confirmation_instead_of_saving(self):
        attendance_date = date.today() - timedelta(days=1)
        self._post_create(self.flexible_employee, attendance_date)
        self.assertEqual(
            Attendance.objects.filter(employee_id=self.flexible_employee).count(), 1
        )

        response = self._post_create(
            self.flexible_employee, attendance_date, attendance_clock_in="10:00",
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Confirm Override")
        attendance = Attendance.objects.get(employee_id=self.flexible_employee)
        # Unchanged -- the second submission was intercepted before saving.
        self.assertEqual(attendance.attendance_clock_in.strftime("%H:%M"), "09:00")

    def test_confirmed_override_replaces_the_prior_record(self):
        attendance_date = date.today() - timedelta(days=1)
        self._post_create(self.flexible_employee, attendance_date)

        response = self._post_create(
            self.flexible_employee,
            attendance_date,
            attendance_clock_in="10:00",
            confirm_override="true",
        )
        self.assertEqual(response.status_code, 200)
        attendance = Attendance.objects.get(employee_id=self.flexible_employee)
        self.assertEqual(attendance.attendance_clock_in.strftime("%H:%M"), "10:00")
        self.assertEqual(
            AttendanceActivity.objects.filter(employee_id=self.flexible_employee).count(), 1
        )
        self.assertEqual(
            AttendanceActivityLog.objects.filter(
                action_type=AttendanceActivityLog.ACTION_MANUAL_CREATE_OVERRIDE,
                affected_employee_ids=[self.flexible_employee.pk],
            ).count(),
            2,
        )


class BatchByEmployeesTests(CreateAttendanceWebTestBase):
    """
    Batch Entry -- "Multiple Employees, One Date" (attendance/cbv/
    create_attendance.py::CreateAttendanceBatchByEmployeesView).
    """

    BATCH_URL = "/attendance/create-attendance/batch-by-employees/"

    def setUp(self):
        super().setUp()
        self.second_flexible_employee = make_employee(
            company=self.company,
            email="flexible_2@test.horilla",
            user=make_user("flexible_2"),
        )

    def _post_batch(self, attendance_date, rows, **extra):
        payload = {
            "attendance_date": attendance_date.isoformat(),
            "employee_id": [str(r.get("employee_id", "")) for r in rows],
            "clock_in": [r.get("clock_in", "09:00") for r in rows],
            "clock_out": [r.get("clock_out", "18:00") for r in rows],
            "reason": [r.get("reason", "Batch entry") for r in rows],
            "shift_id": [str(r.get("shift_id", "")) for r in rows],
        }
        payload.update(extra)
        return self.client.post(self.BATCH_URL, payload, **self.HX)

    def test_get_renders_the_row_builder(self):
        response = self.client.get(self.BATCH_URL, **self.HX)
        self.assertEqual(response.status_code, 200)

    def test_creates_one_record_per_employee_row(self):
        attendance_date = date.today() - timedelta(days=1)
        response = self._post_batch(
            attendance_date,
            [
                {"employee_id": self.flexible_employee.pk},
                {"employee_id": self.second_flexible_employee.pk},
            ],
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            Attendance.objects.filter(
                attendance_date=attendance_date,
                creation_source=Attendance.CREATION_SOURCE_MANUAL,
            ).count(),
            2,
        )
        self.assertEqual(
            AttendanceActivityLog.objects.filter(
                action_type=AttendanceActivityLog.ACTION_MANUAL_CREATE_OVERRIDE,
                source="Create Attendance (Batch)",
            ).count(),
            1,
        )
        log = AttendanceActivityLog.objects.get(source="Create Attendance (Batch)")
        self.assertCountEqual(
            log.affected_employee_ids,
            [self.flexible_employee.pk, self.second_flexible_employee.pk],
        )

    def test_shift_based_employee_in_batch_requires_a_shift(self):
        attendance_date = date.today() - timedelta(days=1)
        response = self._post_batch(
            attendance_date, [{"employee_id": self.shift_employee.pk}]
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            Attendance.objects.filter(employee_id=self.shift_employee).exists()
        )
        self.assertContains(response, "shift is required")

    def test_duplicate_employee_in_same_batch_is_rejected(self):
        attendance_date = date.today() - timedelta(days=1)
        response = self._post_batch(
            attendance_date,
            [
                {"employee_id": self.flexible_employee.pk},
                {"employee_id": self.flexible_employee.pk},
            ],
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "listed more than once")
        self.assertFalse(
            Attendance.objects.filter(employee_id=self.flexible_employee).exists()
        )

    def test_existing_records_prompt_an_aggregated_confirmation(self):
        attendance_date = date.today() - timedelta(days=1)
        self._post_batch(attendance_date, [{"employee_id": self.flexible_employee.pk}])
        self.assertEqual(
            Attendance.objects.filter(employee_id=self.flexible_employee).count(), 1
        )

        response = self._post_batch(
            attendance_date,
            [
                {"employee_id": self.flexible_employee.pk, "clock_in": "10:00"},
                {"employee_id": self.second_flexible_employee.pk},
            ],
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Confirm Override")
        # Only the first employee's record exists -- unconfirmed, nothing
        # from this batch (including the non-conflicting second row)
        # should have been saved yet.
        self.assertFalse(
            Attendance.objects.filter(employee_id=self.second_flexible_employee).exists()
        )

        confirm_response = self._post_batch(
            attendance_date,
            [
                {"employee_id": self.flexible_employee.pk, "clock_in": "10:00"},
                {"employee_id": self.second_flexible_employee.pk},
            ],
            confirm_override="true",
        )
        self.assertEqual(confirm_response.status_code, 200)
        self.assertEqual(
            Attendance.objects.get(employee_id=self.flexible_employee)
            .attendance_clock_in.strftime("%H:%M"),
            "10:00",
        )
        self.assertTrue(
            Attendance.objects.filter(employee_id=self.second_flexible_employee).exists()
        )


class BatchByDatesTests(CreateAttendanceWebTestBase):
    """
    Batch Entry -- "One Employee, Multiple Dates" (attendance/cbv/
    create_attendance.py::CreateAttendanceBatchByDatesView).
    """

    BATCH_URL = "/attendance/create-attendance/batch-by-dates/"

    def _post_batch(self, employee, rows, **extra):
        payload = {
            "employee_id": str(employee.pk),
            "attendance_date": [r["attendance_date"].isoformat() for r in rows],
            "clock_in": [r.get("clock_in", "09:00") for r in rows],
            "clock_out": [r.get("clock_out", "18:00") for r in rows],
            "reason": [r.get("reason", "Batch entry") for r in rows],
            "shift_id": [str(r.get("shift_id", "")) for r in rows],
        }
        payload.update(extra)
        return self.client.post(self.BATCH_URL, payload, **self.HX)

    def test_get_renders_the_row_builder(self):
        response = self.client.get(self.BATCH_URL, **self.HX)
        self.assertEqual(response.status_code, 200)

    def test_creates_one_record_per_date_row(self):
        day_one = date.today() - timedelta(days=2)
        day_two = date.today() - timedelta(days=1)
        response = self._post_batch(
            self.flexible_employee,
            [{"attendance_date": day_one}, {"attendance_date": day_two}],
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            Attendance.objects.filter(
                employee_id=self.flexible_employee,
                creation_source=Attendance.CREATION_SOURCE_MANUAL,
            ).count(),
            2,
        )
        log = AttendanceActivityLog.objects.get(source="Create Attendance (Batch)")
        self.assertEqual(log.affected_employee_ids, [self.flexible_employee.pk])

    def test_duplicate_date_in_same_batch_is_rejected(self):
        day = date.today() - timedelta(days=1)
        response = self._post_batch(
            self.flexible_employee, [{"attendance_date": day}, {"attendance_date": day}],
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "listed more than once")
        self.assertFalse(
            Attendance.objects.filter(employee_id=self.flexible_employee).exists()
        )

    def test_shift_based_employee_requires_a_shift_on_every_row(self):
        day = date.today() - timedelta(days=1)
        response = self._post_batch(self.shift_employee, [{"attendance_date": day}])
        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            Attendance.objects.filter(employee_id=self.shift_employee).exists()
        )
        self.assertContains(response, "shift is required")
