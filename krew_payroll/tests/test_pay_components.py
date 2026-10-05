"""Earnings / deductions: formula parser, rules and the four write services."""

from datetime import timedelta
from decimal import Decimal

from django.test import TestCase

from horilla.testkit.company import CompanyFilterTestMixin
from horilla.testkit.factories import make_company, make_employee
from krew_payroll.methods.pay_components import formula, rules, services
from krew_payroll.methods.pay_components.services import PayComponentError
from krew_payroll.models.pay_components import Calculation, ComponentType, PublishState

E, D = ComponentType.EARNING, ComponentType.DEDUCTION


def first_of_next_months(n):
    """Today for n=0, else the 1st of the n-th next month."""
    day = rules.today()
    for _ in range(n):
        day = (day.replace(day=28) + timedelta(days=4)).replace(day=1)
    return day


class FormulaTests(TestCase):
    def test_precedence_brackets_and_functions(self):
        env = {"BASIC": Decimal(1000), "DA": Decimal(100)}
        self.assertEqual(formula.evaluate(formula.parse("BASIC + DA * 2"), env), 1200)
        self.assertEqual(formula.evaluate(formula.parse("(BASIC + DA) * 0.4"), env), Decimal("440.0"))
        self.assertEqual(formula.evaluate(formula.parse("max(BASIC - 2000, 0)"), env), 0)
        self.assertEqual(formula.evaluate(formula.parse("min(BASIC, 500, 700)"), env), 500)
        self.assertEqual(formula.evaluate(formula.parse("BASIC / 0"), env), 0)
        self.assertEqual(formula.evaluate(formula.parse("-UNKNOWN + 5"), env), 5)

    def test_references_and_errors(self):
        self.assertEqual(formula.references(formula.parse("max(CTC - BASIC, HRA)")), {"CTC", "BASIC", "HRA"})
        for bad in ("", "BASIC +", "max(BASIC)", "BASIC $ 2", "(BASIC"):
            with self.assertRaises(formula.FormulaError):
                formula.parse(bad)


class ServiceTests(CompanyFilterTestMixin, TestCase):
    def setUp(self):
        self.company = make_company("Sarvam")
        self.set_company_context(self.company.id)
        self.emp = make_employee(company=self.company, email="ravi@test.horilla")

    # helpers
    def create(self, type_, name):
        component, details = services.create_component(type_, {"name": name})
        return component, details

    def patch(self, details, body):
        return services.update_version(details, body)

    def ready_earning(self, name, calc, start=None):
        component, details = self.create(E, name)
        self.patch(details, {"effective_from": (start or first_of_next_months(0)).isoformat(), "calculations": [calc]})
        return component, details

    # 1. create
    def test_create_generates_code_and_draft_v1(self):
        component, details = self.create(E, "Attendance Bonus")
        self.assertEqual(component.code, "AB")
        self.assertEqual(component.company_id, self.company)
        self.assertEqual(details.version_no, 1)
        self.assertEqual(details.publish_state, PublishState.DRAFT)
        self.assertEqual(details.recurring_interval, "EVERY_CYCLE")
        self.assertEqual(details.tax_treatment, "TAXABLE")
        self.assertTrue(Calculation.objects.filter(component_details=details, purpose="COMPONENT_VALUE", mode="FLAT").exists())
        _, ded = self.create(D, "Canteen Recovery")
        self.assertEqual(ded.recurring_interval, "MONTHLY")
        self.assertIsNone(ded.tax_treatment)

    def test_create_rejects_duplicates_and_extra_keys(self):
        self.create(E, "Basic")
        with self.assertRaises(PayComponentError) as ctx:
            services.create_component(E, {"name": "basic"})
        self.assertEqual(ctx.exception.status, 400)
        with self.assertRaises(PayComponentError) as ctx:
            services.create_component(E, {"name": "X Pay", "code": "XX", "frequency": "ONE_TIME"})
        self.assertIn("code", ctx.exception.payload["errors"])
        self.assertIn("frequency", ctx.exception.payload["errors"])
        # Same name is fine for the other type.
        services.create_component(D, {"name": "Basic"})

    def test_code_is_unique_and_avoids_reserved_words(self):
        a, _ = self.create(E, "House Rent Allowance")
        b, _ = self.create(E, "Housing Rent Allowance")
        self.assertEqual((a.code, b.code), ("HRA", "HRA2"))
        c, _ = self.create(E, "CTC")
        self.assertEqual(c.code, "CTCX")

    # 2. update
    def test_draft_saves_incomplete_and_reports_checks(self):
        _, details = self.create(E, "Basic")
        details, checks = self.patch(details, {"payslip_label": "Basic Pay"})
        self.assertEqual(details.payslip_label, "Basic Pay")
        self.assertGreater(checks["total"], 0)
        self.assertIn("calculation", checks["errors"])

    def test_shape_errors_fail_even_for_drafts(self):
        _, details = self.create(E, "Basic")
        with self.assertRaises(PayComponentError) as ctx:
            self.patch(details, {"reduces_taxable_income": True, "code": "X", "foo": 1, "effective_from": "15-01-2030", "effective_to": "2030-13-01"})
        errors = ctx.exception.payload["errors"]
        self.assertEqual(errors["reduces_taxable_income"], ["not a field of an earning"])
        self.assertEqual(errors["code"], ["read-only: set by the server"])
        self.assertEqual(errors["foo"], ["unknown field"])
        self.assertEqual(errors["effective_from"], ["must be YYYY-MM-DD or null"])
        self.assertEqual(errors["effective_to"], ["must be YYYY-MM-DD or null"])

    def test_percentage_base_sets_depends_on(self):
        basic, _ = self.ready_earning("Basic", {"purpose": "COMPONENT_VALUE", "mode": "PERCENTAGE", "rate": 50, "base_includes_ctc": True})
        da, _ = self.ready_earning("Dearness Allowance", {"purpose": "COMPONENT_VALUE", "mode": "PERCENTAGE", "rate": 10, "base_component_ids": [basic.id]})
        _, hra = self.create(E, "House Rent Allowance")
        hra, _ = self.patch(hra, {"calculations": [{"purpose": "COMPONENT_VALUE", "mode": "PERCENTAGE", "rate": 40, "base_component_ids": [basic.id, da.id]}]})
        self.assertEqual(hra.depends_on, [basic.id, da.id])
        _, spl = self.create(E, "Special Allowance")
        spl, _ = self.patch(spl, {"calculations": [{"purpose": "COMPONENT_VALUE", "mode": "FORMULA", "formula": "max(CTC - BASIC - DA, 0)"}]})
        self.assertEqual(spl.depends_on, [basic.id, da.id])

    def test_mode_fields_are_normalised(self):
        _, details = self.create(E, "Basic")
        details, _ = self.patch(details, {"calculations": [{"purpose": "COMPONENT_VALUE", "mode": "FLAT", "flat_amount": 1000, "rate": 50, "formula": "X"}]})
        calc = details.calculations.get(purpose="COMPONENT_VALUE")
        self.assertEqual((calc.flat_amount, calc.rate, calc.formula), (Decimal("1000.00"), None, None))

    # 3. publish
    def test_publish_flow_and_version_closing(self):
        component, v1 = self.ready_earning("Basic", {"purpose": "COMPONENT_VALUE", "mode": "FLAT", "flat_amount": 1000})
        v1, closed, _ = services.publish_version(v1)
        self.assertEqual((v1.publish_state, closed), (PublishState.PUBLISHED, []))
        with self.assertRaises(PayComponentError) as ctx:
            services.publish_version(v1)
        self.assertEqual(ctx.exception.status, 409)
        v2 = services.new_version(component)
        self.assertEqual((v2.version_no, v2.publish_state), (2, PublishState.DRAFT))
        self.assertEqual(v2.calculations.get().flat_amount, Decimal("1000.00"))
        start = first_of_next_months(3)
        self.patch(v2, {"effective_from": start.isoformat()})
        v2, closed, _ = services.publish_version(v2)
        v1.refresh_from_db()
        self.assertEqual(v1.effective_to, start - timedelta(days=1))
        self.assertEqual([c.pk for c in closed], [v1.pk])

    def test_publish_checks_block_and_write_nothing(self):
        _, details = self.create(E, "Basic")
        with self.assertRaises(PayComponentError) as ctx:
            services.publish_version(details)
        self.assertEqual(ctx.exception.status, 400)
        details.refresh_from_db()
        self.assertEqual(details.publish_state, PublishState.DRAFT)

    def test_published_edit_must_pass_checks_or_rolls_back(self):
        _, v1 = self.ready_earning("Basic", {"purpose": "COMPONENT_VALUE", "mode": "FLAT", "flat_amount": 1000})
        services.publish_version(v1)
        with self.assertRaises(PayComponentError):
            self.patch(v1, {"max_amount": 5, "calculations": [{"purpose": "COMPONENT_VALUE", "mode": "FLAT", "flat_amount": 0}]})
        v1.refresh_from_db()
        self.assertIsNone(v1.max_amount)
        self.assertEqual(v1.calculations.get().flat_amount, Decimal("1000.00"))
        v1, _ = self.patch(v1, {"max_amount": 900})
        self.assertEqual(v1.max_amount, Decimal("900.00"))

    def test_circular_reference_is_caught(self):
        a, va = self.create(E, "Alpha Pay")
        b, vb = self.create(E, "Beta Pay")
        self.patch(va, {"calculations": [{"purpose": "COMPONENT_VALUE", "mode": "PERCENTAGE", "rate": 10, "base_component_ids": [b.id]}]})
        vb, checks = self.patch(vb, {"calculations": [{"purpose": "COMPONENT_VALUE", "mode": "FORMULA", "formula": f"{a.code} * 2"}]})
        self.assertTrue(any("Circular reference" in e for e in checks["errors"]["calculation"]))

    def test_specific_mode_needs_include_list_and_keeps_one_list(self):
        _, details = self.create(D, "Scanner Damage Recovery")
        with self.assertRaises(PayComponentError):
            self.patch(details, {"eligibility_mode": "SPECIFIC", "employees": [{"employee_id": self.emp.id, "list_type": "EXCLUDE"}]})
        details, _ = self.patch(details, {"eligibility_mode": "SPECIFIC", "employees": [{"employee_id": self.emp.id, "list_type": "INCLUDE"}]})
        self.assertEqual(list(details.employee_lists.values_list("list_type", flat=True)), ["INCLUDE"])
        details, _ = self.patch(details, {"eligibility_mode": "ALL"})
        self.assertFalse(details.employee_lists.exists())

    # 4. deactivate
    def test_deactivate_lists_dependants(self):
        basic, v = self.ready_earning("Basic", {"purpose": "COMPONENT_VALUE", "mode": "FLAT", "flat_amount": 1000})
        services.publish_version(v)
        _, hra = self.create(E, "House Rent Allowance")
        self.patch(hra, {"calculations": [{"purpose": "COMPONENT_VALUE", "mode": "PERCENTAGE", "rate": 40, "base_component_ids": [basic.id]}]})
        dependants = services.deactivate_component(basic)
        basic.refresh_from_db()
        self.assertFalse(basic.is_active)
        self.assertEqual([d.code for d in dependants], ["HRA"])
        self.assertEqual(services.deactivate_component(basic)[0].code, "HRA")

    # company scoping
    def test_other_company_is_not_found(self):
        component, _ = self.create(E, "Basic")
        other = make_company("Other Co")
        self.set_company_context(other.id)
        with self.assertRaises(PayComponentError) as ctx:
            services.get_component(E, component.id)
        self.assertEqual(ctx.exception.status, 404)
        self.set_company_context(self.company.id)
        with self.assertRaises(PayComponentError):
            services.get_component(D, component.id)
        self.assertEqual(services.get_component(E, component.id), component)

    def test_live_version_column(self):
        from krew_payroll.models.pay_components import PayComponentDetails

        today = rules.today()
        component, v1 = self.ready_earning("Basic", {"purpose": "COMPONENT_VALUE", "mode": "FLAT", "flat_amount": 1000})
        self.patch(v1, {"effective_to": today.isoformat()})
        services.publish_version(v1)
        self.assertEqual(component.get_version_col(), f"v1 · {today:%d %b %Y} → {today:%d %b %Y}")
        # A draft with other rules doesn't leak into the row: every column is v1's.
        v2 = services.new_version(component)
        self.patch(v2, {"display_order": 9, "tax_treatment": "TAX_FREE", "exemption_type": "FULL",
                        "calculations": [{"purpose": "COMPONENT_VALUE", "mode": "FLAT", "flat_amount": 5000}]})
        row = [component.get_frequency_col(), str(component.get_tax_col()), component.get_calculation_col(), component.get_order_col()]
        v1.refresh_from_db()
        self.assertEqual(row, ["Every Cycle", "Taxable", "Flat ₹1,000", v1.display_order])
        self.assertNotEqual(v1.display_order, 9)
        # Ended yesterday: not shown as live, nor as upcoming.
        yesterday = today - timedelta(days=1)
        PayComponentDetails.objects.entire().filter(pk=v1.pk).update(effective_from=yesterday, effective_to=yesterday)
        self.assertEqual(component.get_version_col(), "")  # no live version: blank
        row = [component.get_frequency_col(), component.get_tax_col(), component.get_calculation_col(), component.get_order_col()]
        self.assertEqual(row, ["", "", "", ""])
        # A later version waiting to start.
        later = today + timedelta(days=10)
        PayComponentDetails.objects.entire().filter(pk=v1.pk).update(effective_from=later, effective_to=None)
        self.assertEqual(component.get_version_col(), f"v1 · starts {later:%d %b %Y}")  # upcoming is shown
        self.assertEqual(component.get_calculation_col(), "Flat ₹1,000")

    def test_any_day_including_past_dates(self):
        component, v1 = self.ready_earning("Basic", {"purpose": "COMPONENT_VALUE", "mode": "FLAT", "flat_amount": 1000})
        services.publish_version(v1)
        v2 = services.new_version(component)
        mid = rules.today() + timedelta(days=10)
        self.patch(v2, {"effective_from": mid.isoformat(), "effective_to": (mid + timedelta(days=45)).isoformat()})
        v2, closed, _ = services.publish_version(v2)
        v1.refresh_from_db()
        self.assertEqual((v2.effective_from, v1.effective_to), (mid, mid - timedelta(days=1)))

    def test_back_dated_component_applies_to_past_periods(self):
        from krew_payroll.methods.pay_components import engine

        past = rules.today() - timedelta(days=40)
        component, v1 = self.ready_earning("Arrears Bonus", {"purpose": "COMPONENT_VALUE", "mode": "FLAT", "flat_amount": 500}, start=past)
        v1, _, warnings = services.publish_version(v1)
        self.assertEqual(v1.publish_state, PublishState.PUBLISHED)
        self.assertTrue(any("not recalculated" in w for w in warnings))
        config = engine.load_configuration(self.company.id, past, past + timedelta(days=5))
        self.assertIn(component.id, [run.component.id for run in config["earnings"]])
        before = engine.load_configuration(self.company.id, past - timedelta(days=30), past - timedelta(days=1))
        self.assertNotIn(component.id, [run.component.id for run in before["earnings"]])
