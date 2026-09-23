"""
Tests for the tiered config framework (attendance/config_tiers.py) against
its first concrete model, AttendanceRuleSet, and for PendingConfigChange's
scheduling/apply/cancel/retry behavior.
"""

from datetime import date

from django.test import TestCase

from attendance.models import AttendanceRuleSet, PendingConfigChange
from base.models import COLLAR_BLUE, COLLAR_WHITE, Company, Department, EmployeeType
from employee.models import Employee, EmployeeWorkInformation
from horilla.testkit.factories import make_company, make_employee, make_user


class AttendanceRuleSetResolutionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = make_company("Acme")
        cls.other_company = make_company("Globex")

        cls.dept = Department.objects.create(department="Warehouse")
        cls.dept.company_id.add(cls.company)
        cls.other_dept = Department.objects.create(department="Office")
        cls.other_dept.company_id.add(cls.company)

        cls.emp_type = EmployeeType.objects.create(
            employee_type="Contract", collar_category=COLLAR_WHITE
        )
        cls.emp_type.company_id.add(cls.company)

        cls.company_default = AttendanceRuleSet.objects.create(
            tier="COMPANY",
            company=cls.company,
            mode=AttendanceRuleSet.MODE_SHIFT_BASED,
            late_grace_minutes=10,
        )

        cls.user = make_user("emp1")
        cls.employee = make_employee(
            company=cls.company, email="emp1@test.horilla", user=cls.user,
            department=cls.dept,
        )

    def test_falls_back_to_company_default(self):
        resolved = AttendanceRuleSet.resolve_for_employee(self.employee)
        self.assertEqual(resolved, self.company_default)

    def test_employee_type_override_wins_over_company_default(self):
        EmployeeWorkInformation.objects.filter(employee_id=self.employee).update(
            employee_type_id=self.emp_type
        )
        override = AttendanceRuleSet.objects.create(
            tier="EMPLOYEE_TYPE",
            company=self.company,
            employee_type_category=COLLAR_WHITE,
            mode=AttendanceRuleSet.MODE_FLEXIBLE,
        )
        employee = Employee.objects.get(pk=self.employee.pk)
        resolved = AttendanceRuleSet.resolve_for_employee(employee)
        self.assertEqual(resolved, override)

    def test_department_override_wins_over_employee_type_and_company_default(self):
        EmployeeWorkInformation.objects.filter(employee_id=self.employee).update(
            employee_type_id=self.emp_type
        )
        AttendanceRuleSet.objects.create(
            tier="EMPLOYEE_TYPE",
            company=self.company,
            employee_type_category=COLLAR_WHITE,
            mode=AttendanceRuleSet.MODE_FLEXIBLE,
        )
        dept_override = AttendanceRuleSet.objects.create(
            tier="DEPARTMENT",
            company=self.company,
            department=self.dept,
            mode=AttendanceRuleSet.MODE_SHIFT_BASED,
            late_grace_minutes=5,
        )
        resolved = AttendanceRuleSet.resolve_for_employee(self.employee)
        self.assertEqual(resolved, dept_override)

    def test_other_departments_override_never_leaks(self):
        AttendanceRuleSet.objects.create(
            tier="DEPARTMENT",
            company=self.company,
            department=self.other_dept,
            mode=AttendanceRuleSet.MODE_FLEXIBLE,
        )
        resolved = AttendanceRuleSet.resolve_for_employee(self.employee)
        self.assertEqual(resolved, self.company_default)

    def test_no_company_default_returns_none(self):
        other_user = make_user("emp2")
        other_employee = make_employee(
            company=self.other_company, email="emp2@test.horilla", user=other_user,
        )
        resolved = AttendanceRuleSet.resolve_for_employee(other_employee)
        self.assertIsNone(resolved)

    def test_shared_category_does_not_leak_across_companies(self):
        # Same collar_category code ("WHITE_COLLAR") overridden in both
        # companies -- resolution must stay scoped to the employee's own
        # company via AttendanceRuleSet.company, not match the other
        # company's row just because the category code is identical.
        other_default = AttendanceRuleSet.objects.create(
            tier="COMPANY", company=self.other_company,
            mode=AttendanceRuleSet.MODE_FLEXIBLE,
        )
        other_override = AttendanceRuleSet.objects.create(
            tier="EMPLOYEE_TYPE", company=self.other_company,
            employee_type_category=COLLAR_WHITE, mode=AttendanceRuleSet.MODE_FLEXIBLE,
        )
        EmployeeWorkInformation.objects.filter(employee_id=self.employee).update(
            employee_type_id=self.emp_type
        )
        my_override = AttendanceRuleSet.objects.create(
            tier="EMPLOYEE_TYPE", company=self.company,
            employee_type_category=COLLAR_WHITE, mode=AttendanceRuleSet.MODE_SHIFT_BASED,
        )
        employee = Employee.objects.get(pk=self.employee.pk)
        resolved = AttendanceRuleSet.resolve_for_employee(employee)
        self.assertEqual(resolved, my_override)
        self.assertNotEqual(resolved, other_override)

    def test_employee_removed_from_department_falls_through(self):
        dept_override = AttendanceRuleSet.objects.create(
            tier="DEPARTMENT", company=self.company, department=self.dept,
            mode=AttendanceRuleSet.MODE_FLEXIBLE,
        )
        resolved = AttendanceRuleSet.resolve_for_employee(self.employee)
        self.assertEqual(resolved, dept_override)

        EmployeeWorkInformation.objects.filter(employee_id=self.employee).update(
            department_id=None
        )
        employee = Employee.objects.get(pk=self.employee.pk)
        resolved_after = AttendanceRuleSet.resolve_for_employee(employee)
        self.assertEqual(resolved_after, self.company_default)


class AttendanceRuleSetInheritedFieldsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = make_company("Acme")
        cls.emp_type = EmployeeType.objects.create(
            employee_type="Contract", collar_category=COLLAR_WHITE
        )
        cls.emp_type.company_id.add(cls.company)
        cls.company_default = AttendanceRuleSet.objects.create(
            tier="COMPANY", company=cls.company,
            mode=AttendanceRuleSet.MODE_SHIFT_BASED, late_grace_minutes=15,
        )

    def test_employee_type_override_inherits_blank_rule_values(self):
        override = AttendanceRuleSet.objects.create(
            tier="EMPLOYEE_TYPE", company=self.company,
            employee_type_category=COLLAR_WHITE, mode=AttendanceRuleSet.MODE_FLEXIBLE,
        )
        values = override.get_effective_values()
        self.assertEqual(values["mode"], AttendanceRuleSet.MODE_FLEXIBLE)
        self.assertEqual(values["late_grace_minutes"], 15)

    def test_department_override_full_values_are_not_overwritten(self):
        dept = Department.objects.create(department="Warehouse")
        dept.company_id.add(self.company)
        override = AttendanceRuleSet.objects.create(
            tier="DEPARTMENT", company=self.company, department=dept,
            mode=AttendanceRuleSet.MODE_SHIFT_BASED, late_grace_minutes=2,
        )
        values = override.get_effective_values()
        self.assertEqual(values["late_grace_minutes"], 2)


class AttendanceRuleSetConstraintTests(TestCase):
    def setUp(self):
        self.company = make_company("Acme")
        self.emp_type = EmployeeType.objects.create(
            employee_type="Contract", collar_category=COLLAR_WHITE
        )
        self.emp_type.company_id.add(self.company)

    def test_second_company_default_rejected(self):
        AttendanceRuleSet.objects.create(tier="COMPANY", company=self.company)
        with self.assertRaises(Exception):
            AttendanceRuleSet.objects.create(tier="COMPANY", company=self.company)

    def test_second_employee_type_override_for_same_category_rejected(self):
        AttendanceRuleSet.objects.create(
            tier="EMPLOYEE_TYPE", company=self.company,
            employee_type_category=COLLAR_WHITE,
        )
        with self.assertRaises(Exception):
            AttendanceRuleSet.objects.create(
                tier="EMPLOYEE_TYPE", company=self.company,
                employee_type_category=COLLAR_WHITE,
            )

    def test_a_different_category_for_the_same_company_is_allowed(self):
        AttendanceRuleSet.objects.create(
            tier="EMPLOYEE_TYPE", company=self.company,
            employee_type_category=COLLAR_WHITE,
        )
        # Should not raise -- a different category is a distinct override.
        AttendanceRuleSet.objects.create(
            tier="EMPLOYEE_TYPE", company=self.company,
            employee_type_category=COLLAR_BLUE,
        )

    def test_clean_rejects_employee_type_without_scope_field(self):
        instance = AttendanceRuleSet(tier="EMPLOYEE_TYPE", company=self.company)
        with self.assertRaises(Exception):
            instance.full_clean()


class PendingConfigChangeTests(TestCase):
    def setUp(self):
        self.company = make_company("Acme")
        self.rule_set = AttendanceRuleSet.objects.create(
            tier="COMPANY", company=self.company,
            mode=AttendanceRuleSet.MODE_SHIFT_BASED, late_grace_minutes=10,
        )

    def test_schedule_defaults_effective_date_to_next_month_first(self):
        change = PendingConfigChange.schedule(
            self.rule_set, {"late_grace_minutes": 20},
        )
        from attendance.config_tiers import next_month_first

        self.assertEqual(change.effective_date, next_month_first())
        self.assertEqual(change.status, PendingConfigChange.STATUS_PENDING)
        self.assertEqual(change.previous_values, {"late_grace_minutes": 10})

    def test_apply_updates_target_and_marks_applied(self):
        change = PendingConfigChange.schedule(
            self.rule_set, {"late_grace_minutes": 20},
        )
        change.apply()
        self.rule_set.refresh_from_db()
        self.assertEqual(self.rule_set.late_grace_minutes, 20)
        self.assertEqual(change.status, PendingConfigChange.STATUS_APPLIED)
        self.assertIsNotNone(change.applied_at)

    def test_apply_is_a_no_op_the_second_time(self):
        change = PendingConfigChange.schedule(
            self.rule_set, {"late_grace_minutes": 20},
        )
        change.apply()
        applied_at_first = change.applied_at

        # Simulate a stray retry after success: value changed again in the
        # meantime by something else, apply() must not touch it.
        self.rule_set.late_grace_minutes = 99
        self.rule_set.save(update_fields=["late_grace_minutes"])
        change.apply()

        self.rule_set.refresh_from_db()
        self.assertEqual(self.rule_set.late_grace_minutes, 99)
        self.assertEqual(change.applied_at, applied_at_first)

    def test_scheduling_a_second_change_cancels_the_first(self):
        first = PendingConfigChange.schedule(
            self.rule_set, {"late_grace_minutes": 20},
        )
        second = PendingConfigChange.schedule(
            self.rule_set, {"late_grace_minutes": 30},
        )
        first.refresh_from_db()
        self.assertEqual(first.status, PendingConfigChange.STATUS_CANCELLED)
        self.assertEqual(second.status, PendingConfigChange.STATUS_PENDING)

        second.apply()
        self.rule_set.refresh_from_db()
        self.assertEqual(self.rule_set.late_grace_minutes, 30)

    def test_cancel_prevents_a_pending_change_from_ever_applying(self):
        change = PendingConfigChange.schedule(
            self.rule_set, {"late_grace_minutes": 20},
        )
        change.cancel()
        change.apply()
        self.rule_set.refresh_from_db()
        self.assertEqual(self.rule_set.late_grace_minutes, 10)
        self.assertEqual(change.status, PendingConfigChange.STATUS_CANCELLED)

    def test_new_override_creation_goes_through_the_same_delay(self):
        dept = Department.objects.create(department="Warehouse")
        dept.company_id.add(self.company)
        placeholder = AttendanceRuleSet.objects.create(
            tier="DEPARTMENT", company=self.company, department=dept,
            is_active=False,
        )
        change = PendingConfigChange.schedule(
            placeholder,
            {"is_active": True, "mode": AttendanceRuleSet.MODE_FLEXIBLE},
        )
        self.assertFalse(
            AttendanceRuleSet.objects.get(pk=placeholder.pk).is_active
        )
        change.apply()
        placeholder.refresh_from_db()
        self.assertTrue(placeholder.is_active)
        self.assertEqual(placeholder.mode, AttendanceRuleSet.MODE_FLEXIBLE)


class NextMonthFirstTests(TestCase):
    def test_returns_next_month_even_when_called_on_the_first(self):
        from attendance.config_tiers import next_month_first

        self.assertEqual(next_month_first(date(2026, 3, 1)), date(2026, 4, 1))

    def test_mid_month(self):
        from attendance.config_tiers import next_month_first

        self.assertEqual(next_month_first(date(2026, 3, 15)), date(2026, 4, 1))

    def test_december_rolls_into_next_year(self):
        from attendance.config_tiers import next_month_first

        self.assertEqual(next_month_first(date(2026, 12, 10)), date(2027, 1, 1))


class ApplyPendingConfigChangesSchedulerJobTests(TestCase):
    def test_applies_due_changes_and_skips_future_ones(self):
        from attendance.scheduler import apply_pending_config_changes

        company = make_company("Acme")
        rule_set = AttendanceRuleSet.objects.create(
            tier="COMPANY", company=company,
            mode=AttendanceRuleSet.MODE_SHIFT_BASED, late_grace_minutes=10,
        )
        due_change = PendingConfigChange.schedule(
            rule_set, {"late_grace_minutes": 20},
            effective_date=date.today(),
        )
        future_change = PendingConfigChange.schedule(
            AttendanceRuleSet.objects.create(
                tier="COMPANY",
                company=make_company("Globex"),
                late_grace_minutes=5,
            ),
            {"late_grace_minutes": 50},
            effective_date=date(2099, 1, 1),
        )

        apply_pending_config_changes()

        due_change.refresh_from_db()
        future_change.refresh_from_db()
        rule_set.refresh_from_db()
        self.assertEqual(rule_set.late_grace_minutes, 20)
        self.assertEqual(due_change.status, PendingConfigChange.STATUS_APPLIED)
        self.assertEqual(future_change.status, PendingConfigChange.STATUS_PENDING)

    def test_running_twice_is_idempotent(self):
        from attendance.scheduler import apply_pending_config_changes

        company = make_company("Acme")
        rule_set = AttendanceRuleSet.objects.create(
            tier="COMPANY", company=company, late_grace_minutes=10,
        )
        PendingConfigChange.schedule(
            rule_set, {"late_grace_minutes": 20}, effective_date=date.today(),
        )
        apply_pending_config_changes()
        rule_set.late_grace_minutes = 77
        rule_set.save(update_fields=["late_grace_minutes"])
        apply_pending_config_changes()
        rule_set.refresh_from_db()
        self.assertEqual(rule_set.late_grace_minutes, 77)
