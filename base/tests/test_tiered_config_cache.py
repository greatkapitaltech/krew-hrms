"""
Caching for TieredConfigResolutionMixin.resolve_for_employee() -- Part 3
Sec 3.3 of the Attendance performance plan (base/config_tiers.py).
"""

from django.core.cache import cache
from django.test import TestCase

from attendance.models import AttendanceRuleSet
from base.config_tiers import TIER_COMPANY, TIER_DEPARTMENT
from base.models import Department
from horilla.testkit.factories import make_company, make_employee, make_user


class TieredConfigCacheTests(TestCase):
    def setUp(self):
        cache.clear()
        self.company = make_company("Tiered Cache Co")
        self.employee = make_employee(
            company=self.company,
            email="tiered_cache@test.horilla",
            user=make_user("tiered_cache_emp"),
        )
        self.company_default = AttendanceRuleSet.objects.create(
            tier=TIER_COMPANY, company=self.company, regularization_enabled=False,
        )

    def test_resolves_the_company_default_and_caches_it(self):
        row = AttendanceRuleSet.resolve_for_employee(self.employee)
        self.assertEqual(row.pk, self.company_default.pk)

        row_again = AttendanceRuleSet.resolve_for_employee(self.employee)
        self.assertEqual(row_again.pk, self.company_default.pk)

    def test_editing_the_company_default_is_reflected_on_the_next_resolve(self):
        AttendanceRuleSet.resolve_for_employee(self.employee)  # warm the cache

        self.company_default.regularization_enabled = True
        self.company_default.save()

        resolved = AttendanceRuleSet.resolve_for_employee(self.employee)
        self.assertTrue(resolved.regularization_enabled)

    def test_adding_a_department_override_after_caching_that_same_key_invalidates_it(self):
        department = Department.objects.create(department="Tiered Cache Dept 2")
        from employee.models import Employee, EmployeeWorkInformation

        EmployeeWorkInformation.objects.filter(employee_id=self.employee).update(
            department_id=department
        )
        employee = Employee.objects.get(pk=self.employee.pk)

        # Warm the cache for (company, department) with no override yet --
        # falls through to Company Default.
        first = AttendanceRuleSet.resolve_for_employee(employee)
        self.assertEqual(first.pk, self.company_default.pk)

        override = AttendanceRuleSet.objects.create(
            tier=TIER_DEPARTMENT, company=self.company, department=department,
            regularization_enabled=True,
        )

        # Same cache key as the first call (same company/department/type) --
        # must now resolve to the new override, not the stale Company
        # Default pk cached a moment ago.
        second = AttendanceRuleSet.resolve_for_employee(employee)
        self.assertEqual(second.pk, override.pk)

    def test_different_companies_do_not_share_a_cache_entry(self):
        other_company = make_company("Tiered Cache Co 2")
        other_default = AttendanceRuleSet.objects.create(
            tier=TIER_COMPANY, company=other_company, regularization_enabled=True,
        )
        other_employee = make_employee(
            company=other_company,
            email="tiered_cache_other@test.horilla",
            user=make_user("tiered_cache_other_emp"),
        )

        row_a = AttendanceRuleSet.resolve_for_employee(self.employee)
        row_b = AttendanceRuleSet.resolve_for_employee(other_employee)

        self.assertEqual(row_a.pk, self.company_default.pk)
        self.assertEqual(row_b.pk, other_default.pk)
