"""
Web-view tests for the AttendanceRuleSet settings screen
(attendance/cbv/attendance_rule_set.py) -- the three-tier (Company
Default / Employee-Type Override / Department Override) combined rule
set covering #1 Attendance Type, Validation Threshold, the Overtime
cluster, and Regularization's enable/cap.

Every save here has to route through PendingConfigChange rather than
mutating AttendanceRuleSet directly -- these tests exist mainly to pin
that down end-to-end (create, edit, duplicate-scope rejection, the
Employee-Type mode-only restriction, and a not-yet-active row's edit
still carrying its activation forward), not just unit-test the form in
isolation.
"""

from django.test import TestCase

from attendance.forms import ATTENDANCE_RULE_SET_EDITABLE_FIELDS
from attendance.models import AttendanceRuleSet, PendingConfigChange
from base.config_tiers import TIER_COMPANY, TIER_DEPARTMENT, TIER_EMPLOYEE_TYPE
from base.models import Department
from horilla.horilla_middlewares import _thread_locals
from horilla.testkit.company import clear_selected_company
from horilla.testkit.factories import make_company, make_employee, make_user


def make_department(name, *, company):
    department = Department.objects.create(department=name)
    department.company_id.add(company)
    return department


class AttendanceRuleSetWebTestBase(TestCase):
    PAGE_URL = "/attendance/attendance-rule-sets/"
    LIST_URL = "/attendance/attendance-rule-sets/list/"
    NAV_URL = "/attendance/attendance-rule-sets/nav/"
    CREATE_URL = "/attendance/attendance-rule-sets/create/"

    HX = {"HTTP_HX_REQUEST": "true"}

    def update_url(self, pk):
        return f"/attendance/attendance-rule-sets/{pk}/update/"

    def setUp(self):
        # Same ContextVar-leak precaution as test_regularization_web.py --
        # a prior test's authenticated request leaves both
        # _thread_locals.request and the current_company_id ContextVar
        # set, which poisons both created_by and any
        # HorillaCompanyManager-scoped read in this test otherwise.
        _thread_locals.request = None
        clear_selected_company()
        self.company = make_company("Rule Set Web Co")
        self.addCleanup(self._clear_context_vars)

        self.admin_user = make_user("ruleset_web_admin", is_superuser=True)
        self.admin = make_employee(
            company=self.company,
            email="ruleset_web_admin@test.horilla",
            user=self.admin_user,
        )
        self.client.force_login(self.admin_user)
        session = self.client.session
        session["selected_company"] = str(self.company.pk)
        session.save()

    def _clear_context_vars(self):
        _thread_locals.request = None
        clear_selected_company()


class PageAccessTests(AttendanceRuleSetWebTestBase):
    def test_page_renders_for_a_selected_company(self):
        response = self.client.get(self.PAGE_URL)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Select a single company")

    def test_page_shows_a_prompt_when_no_single_company_is_selected(self):
        session = self.client.session
        session["selected_company"] = "all"
        session.save()
        response = self.client.get(self.PAGE_URL)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Select a single company")

    def test_list_and_nav_render_with_zero_rows(self):
        response = self.client.get(self.LIST_URL, **self.HX)
        self.assertEqual(response.status_code, 200)
        response = self.client.get(self.NAV_URL, **self.HX)
        self.assertEqual(response.status_code, 200)

    def test_non_htmx_form_request_is_rejected(self):
        # HorillaFormView is decorated hx_request_required project-wide --
        # a direct (non-HTMX) GET must not reach the view.
        response = self.client.get(self.CREATE_URL)
        self.assertEqual(response.status_code, 405)


class CreateCompanyDefaultTests(AttendanceRuleSetWebTestBase):
    def test_create_makes_an_inactive_row_and_schedules_its_activation(self):
        response = self.client.post(
            self.CREATE_URL,
            data={
                "tier": TIER_COMPANY,
                "mode": "SHIFT_BASED",
                "regularization_enabled": "true",
                "regularization_monthly_cap": "3",
            },
            **self.HX,
        )
        self.assertEqual(response.status_code, 200)

        row = AttendanceRuleSet.objects.get(company=self.company, tier=TIER_COMPANY)
        self.assertFalse(row.is_active)

        pending = PendingConfigChange.objects.get(
            object_id=row.pk, status=PendingConfigChange.STATUS_PENDING
        )
        self.assertTrue(pending.changes["is_active"])
        self.assertEqual(pending.changes["mode"], "SHIFT_BASED")
        self.assertEqual(pending.changes["regularization_monthly_cap"], 3)

    def test_late_grace_minutes_is_not_exposed_by_this_screen(self):
        # Backend stays fully wired (RULE_FIELDS/INHERITED_FIELDS,
        # resolution, snapshotting -- see test_attendance_rule_set.py)
        # but the form doesn't render it, and posting it anyway has no
        # effect: GraceTime is still what actually governs late-mark
        # grace, not this screen. See ATTENDANCE_RULE_SET_EDITABLE_FIELDS's
        # comment in attendance/forms.py.
        get_response = self.client.get(self.CREATE_URL, **self.HX)
        self.assertNotIn(b'name="late_grace_minutes"', get_response.content)

        self.client.post(
            self.CREATE_URL,
            data={"tier": TIER_COMPANY, "mode": "SHIFT_BASED", "late_grace_minutes": "10"},
            **self.HX,
        )
        row = AttendanceRuleSet.objects.get(company=self.company, tier=TIER_COMPANY)
        pending = PendingConfigChange.objects.get(object_id=row.pk)
        self.assertNotIn("late_grace_minutes", pending.changes)

    def test_applying_the_pending_change_activates_the_row(self):
        self.client.post(
            self.CREATE_URL, data={"tier": TIER_COMPANY, "mode": "FLEXIBLE"}, **self.HX
        )
        row = AttendanceRuleSet.objects.get(company=self.company, tier=TIER_COMPANY)
        pending = PendingConfigChange.objects.get(object_id=row.pk)

        pending.apply()
        row.refresh_from_db()

        self.assertTrue(row.is_active)
        self.assertEqual(row.mode, "FLEXIBLE")
        self.assertEqual(
            AttendanceRuleSet.objects.filter(
                company=self.company, tier=TIER_COMPANY, is_active=True
            ).count(),
            1,
        )

    def test_a_second_company_default_is_rejected_not_stacked(self):
        self.client.post(
            self.CREATE_URL, data={"tier": TIER_COMPANY, "mode": "SHIFT_BASED"}, **self.HX
        )
        response = self.client.post(
            self.CREATE_URL, data={"tier": TIER_COMPANY, "mode": "FLEXIBLE"}, **self.HX
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            AttendanceRuleSet.objects.filter(
                company=self.company, tier=TIER_COMPANY
            ).count(),
            1,
        )


class EditTests(AttendanceRuleSetWebTestBase):
    def _create_company_default(self):
        self.client.post(
            self.CREATE_URL, data={"tier": TIER_COMPANY, "mode": "SHIFT_BASED"}, **self.HX
        )
        return AttendanceRuleSet.objects.get(company=self.company, tier=TIER_COMPANY)

    def test_editing_an_already_active_row_schedules_a_change(self):
        row = self._create_company_default()
        PendingConfigChange.objects.get(object_id=row.pk).apply()
        row.refresh_from_db()
        self.assertTrue(row.is_active)

        response = self.client.post(
            self.update_url(row.pk),
            data={"tier": TIER_COMPANY, "mode": "FLEXIBLE", "regularization_monthly_cap": "4"},
            **self.HX,
        )
        self.assertEqual(response.status_code, 200)

        pending = PendingConfigChange.objects.get(
            object_id=row.pk, status=PendingConfigChange.STATUS_PENDING
        )
        self.assertEqual(pending.changes["mode"], "FLEXIBLE")
        # Row was already live -- this edit doesn't need to carry
        # is_active forward, and mustn't have flipped it off either.
        row.refresh_from_db()
        self.assertTrue(row.is_active)

    def test_editing_a_not_yet_active_row_still_carries_its_activation(self):
        # Regression test: editing a row before its first (create-time)
        # PendingConfigChange ever applies replaces that pending change
        # (schedule()'s own "never stacked" rule) -- without explicitly
        # carrying is_active forward, the replacement would drop the
        # activation and leave the row permanently inactive even after a
        # "successful" edit.
        row = self._create_company_default()
        self.assertFalse(row.is_active)

        self.client.post(
            self.update_url(row.pk),
            data={"tier": TIER_COMPANY, "mode": "FLEXIBLE", "total_work_hours_reference": "8"},
            **self.HX,
        )

        pending = PendingConfigChange.objects.get(
            object_id=row.pk, status=PendingConfigChange.STATUS_PENDING
        )
        self.assertTrue(pending.changes["is_active"])

        pending.apply()
        row.refresh_from_db()
        self.assertTrue(row.is_active)
        self.assertEqual(row.mode, "FLEXIBLE")

    def test_scope_fields_are_ignored_on_edit_even_if_tampered(self):
        row = self._create_company_default()
        department = make_department("Some Dept", company=self.company)

        self.client.post(
            self.update_url(row.pk),
            data={
                "tier": TIER_DEPARTMENT,
                "department": department.pk,
                "mode": "FLEXIBLE",
            },
            **self.HX,
        )
        row.refresh_from_db()
        pending = PendingConfigChange.objects.get(
            object_id=row.pk, status=PendingConfigChange.STATUS_PENDING
        )
        # tier/department are disabled fields on an edit -- a tampered
        # POST can't move an existing row to a different scope.
        self.assertEqual(row.tier, TIER_COMPANY)
        self.assertIsNone(row.department_id)
        self.assertEqual(pending.tier, TIER_COMPANY)


class EmployeeTypeOverrideTests(AttendanceRuleSetWebTestBase):
    def test_only_mode_applies_other_rule_fields_are_dropped(self):
        response = self.client.post(
            self.CREATE_URL,
            data={
                "tier": TIER_EMPLOYEE_TYPE,
                "employee_type_category": "WHITE_COLLAR",
                "mode": "FLEXIBLE",
                "regularization_enabled": "true",
                "regularization_monthly_cap": "2",
            },
            **self.HX,
        )
        self.assertEqual(response.status_code, 200)

        row = AttendanceRuleSet.objects.get(
            company=self.company, tier=TIER_EMPLOYEE_TYPE
        )
        pending = PendingConfigChange.objects.get(object_id=row.pk)
        self.assertEqual(pending.changes["mode"], "FLEXIBLE")
        for field_name in ATTENDANCE_RULE_SET_EDITABLE_FIELDS:
            if field_name in AttendanceRuleSet.INHERITED_FIELDS:
                self.assertIsNone(pending.changes[field_name])


class DepartmentOverrideTests(AttendanceRuleSetWebTestBase):
    def test_a_department_not_linked_to_this_company_is_rejected(self):
        other_company = make_company("Some Other Co")
        foreign_department = make_department("Foreign Dept", company=other_company)

        response = self.client.post(
            self.CREATE_URL,
            data={
                "tier": TIER_DEPARTMENT,
                "department": foreign_department.pk,
                "mode": "SHIFT_BASED",
            },
            **self.HX,
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            AttendanceRuleSet.objects.filter(
                company=self.company, tier=TIER_DEPARTMENT
            ).exists()
        )

    def test_a_department_override_can_set_every_rule_field(self):
        department = make_department("Ops", company=self.company)
        response = self.client.post(
            self.CREATE_URL,
            data={
                "tier": TIER_DEPARTMENT,
                "department": department.pk,
                "mode": "SHIFT_BASED",
                "shift_ot_auto_approve_buffer_minutes": "15",
                "track_overtime": "true",
                "ot_threshold_hours": "2",
            },
            **self.HX,
        )
        self.assertEqual(response.status_code, 200)
        row = AttendanceRuleSet.objects.get(
            company=self.company, tier=TIER_DEPARTMENT
        )
        pending = PendingConfigChange.objects.get(object_id=row.pk)
        self.assertEqual(pending.changes["shift_ot_auto_approve_buffer_minutes"], 15)
        self.assertTrue(pending.changes["track_overtime"])
