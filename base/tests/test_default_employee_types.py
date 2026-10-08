"""
Tests for:
- the Company post_save signal that auto-seeds the three fixed
  Employee-Type classifications (White/Blue/Grey Collar) the moment a
  company is created -- see base/signals.py::create_default_employee_types.
- EmployeeTypeForm requiring every EmployeeType to map onto one of the
  three fixed collar_category values, while employee_type itself stays a
  free-form name -- see base/models.py::EmployeeType.
"""

from django.test import TestCase

from base.forms import EmployeeTypeForm
from base.models import (
    COLLAR_BLUE,
    COLLAR_CATEGORY_CHOICES,
    COLLAR_WHITE,
    Company,
    EmployeeType,
)


class DefaultEmployeeTypeSeedingTests(TestCase):
    def test_creating_a_company_seeds_exactly_the_three_default_types(self):
        company = Company.objects.create(company="Seed Test Co")

        types = EmployeeType.objects.filter(company_id=company).values_list(
            "collar_category", "employee_type"
        )
        expected = [
            (category, str(label).upper()) for category, label in COLLAR_CATEGORY_CHOICES
        ]
        self.assertCountEqual(types, expected)

    def test_two_companies_get_their_own_separate_rows(self):
        company_a = Company.objects.create(company="Seed Co A")
        company_b = Company.objects.create(company="Seed Co B")

        types_a = set(
            EmployeeType.objects.filter(company_id=company_a).values_list("pk", flat=True)
        )
        types_b = set(
            EmployeeType.objects.filter(company_id=company_b).values_list("pk", flat=True)
        )
        self.assertTrue(types_a.isdisjoint(types_b))
        self.assertEqual(len(types_a), 3)
        self.assertEqual(len(types_b), 3)

    def test_updating_a_company_does_not_reseed(self):
        company = Company.objects.create(company="Seed Test Co Update")
        self.assertEqual(EmployeeType.objects.filter(company_id=company).count(), 3)

        company.company = "Seed Test Co Update Renamed"
        company.save()

        self.assertEqual(EmployeeType.objects.filter(company_id=company).count(), 3)


class EmployeeTypeCollarCategoryMappingTests(TestCase):
    """
    employee_type is free-form, same as before this feature -- a company
    can still name a type "Machine Operator". What's new is
    collar_category: every type, however it's named, must map onto one
    of the three fixed classifications.
    """

    def test_an_arbitrary_name_is_still_allowed(self):
        form = EmployeeTypeForm(
            data={"employee_type": "Machine Operator", "collar_category": COLLAR_BLUE}
        )
        self.assertTrue(form.is_valid())
        instance = form.save(commit=False)
        self.assertEqual(instance.employee_type, "Machine Operator")
        self.assertEqual(instance.collar_category, COLLAR_BLUE)

    def test_missing_collar_category_is_rejected(self):
        form = EmployeeTypeForm(data={"employee_type": "Machine Operator"})
        self.assertFalse(form.is_valid())
        self.assertIn("collar_category", form.errors)

    def test_a_category_outside_the_fixed_set_is_rejected(self):
        form = EmployeeTypeForm(
            data={"employee_type": "Contractor", "collar_category": "CONTRACTOR"}
        )
        self.assertFalse(form.is_valid())
        self.assertIn("collar_category", form.errors)

    def test_one_of_the_three_fixed_categories_is_accepted(self):
        form = EmployeeTypeForm(
            data={"employee_type": "Site Engineer", "collar_category": COLLAR_WHITE}
        )
        self.assertTrue(form.is_valid())
