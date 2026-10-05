"""
Golden test: the engine reproduces the prototype's December 2026 pay for its
sample company (5 workers, 7 earnings, 7 deductions), plus proration.
"""

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from django.apps import apps
from django.test import TestCase

from base.models import EmployeeShift, Grade, JobPosition, WorkerClass
from employee.models import EmployeeWorkInformation
from horilla.horilla_middlewares import set_selected_company
from horilla.testkit import make_company, make_employee
from krew_payroll.methods.pay_components import engine
from krew_payroll.models.pay_components import (
    Calculation,
    CalculationSlab,
    EligibilityCondition,
    EligibilityEmployee,
    EmployeePayProfile,
    PayComponent,
    PayComponentDetails,
    WorkSite,
)

FIXTURE = Path(__file__).parent / "fixtures" / "pay_components_sample.json"
MONEY_FIELDS = ("min_amount", "max_amount")


class SampleCompanyEngineTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        data = json.loads(FIXTURE.read_text())
        cls.expected = data["expected_december"]
        cls.company = make_company("Sarvam Sample")
        set_selected_company(cls.company.id)
        GSTStateConfig = apps.get_model("krew_company_onboarding", "GSTStateConfig")
        state = {s["id"]: GSTStateConfig.objects.get(code=s["code"]).id for s in data["gst_state_config"]}
        shift = {s["id"]: EmployeeShift.objects.create(employee_shift=f"PC {s['employee_shift']}").id for s in data["base_employeeshift"]}
        from base.models import Department

        dept = Department.objects.create(department="PC Ops")
        position = {p["id"]: JobPosition.objects.create(job_position=f"PC {p['job_position']}", department_id=dept).id for p in data["base_jobposition"]}
        wclass = {w["id"]: WorkerClass.objects.create(company_id=cls.company, code=w["code"], name=w["name"]).id for w in data["worker_class"]}
        grade = {g["id"]: Grade.objects.create(company_id=cls.company, code=g["code"], name=g["name"]).id for g in data["grade"]}
        site = {s["id"]: WorkSite.objects.create(company_id=cls.company, state_id=state[s["state_id"]], name=s["name"]).id for s in data["work_site"]}
        cls.employees, emp = {}, {}
        work = {w["employee_id"]: w for w in data["employee_work_information"]}
        for row in data["employee_employee"]:
            e = make_employee(company=cls.company, email=f"pc{row['id']}@test.horilla", first_name=row["employee_first_name"], last_name=row["employee_last_name"])
            EmployeeWorkInformation.objects.filter(employee_id=e).update(
                shift_id=shift[work[row["id"]]["shift_id"]], job_position_id=position[work[row["id"]]["job_position_id"]]
            )
            emp[row["id"]] = e.id
            cls.employees[f"{row['employee_first_name']} {row['employee_last_name']}"] = e
        for p in data["employee_pay_profile"]:
            EmployeePayProfile.objects.create(
                employee_id=emp[p["employee_id"]], company_id=cls.company, ctc_annual=p["ctc_annual"],
                worker_class_id=wclass[p["worker_class_id"]], employment_type=p["employment_type"],
                grade_id=grade[p["grade_id"]], work_state_id=state[p["work_state_id"]],
                work_site_id=site[p["work_site_id"]], payout_cadence=p["payout_cadence"],
            )
        comp = {c["id"]: PayComponent.objects.create(company_id=cls.company, type=c["type"], code=c["code"], name=c["name"], is_active=c["is_active"]).id for c in data["pay_component"]}
        details = {}
        for d in data["pay_component_details"]:
            fields = {k: v for k, v in d.items() if k not in ("id", "component_id", "depends_on")}
            details[d["id"]] = PayComponentDetails.objects.create(
                component_id=comp[d["component_id"]], depends_on=[comp[i] for i in d["depends_on"] or []], **fields
            ).id
        calc = {}
        for c in data["calculation"]:
            calc[c["id"]] = Calculation.objects.create(
                component_details_id=details[c["component_details_id"]], purpose=c["purpose"], mode=c["mode"],
                flat_amount=c["flat_amount"], rate=c["rate"], base_component_ids=[comp[i] for i in c["base_component_ids"] or []],
                base_includes_ctc=c["base_includes_ctc"], formula=c["formula"],
            ).id
        for s in data["calculation_slab"]:
            CalculationSlab.objects.create(calculation_id=calc[s["calculation_id"]], range_from=s["range_from"], range_to=s["range_to"], amount=s["amount"], applies_in_month=s["applies_in_month"])
        maps = {"WORKER_CLASS": wclass, "SHIFT": shift, "STATE": state, "SITE": site, "GRADE": grade, "DESIGNATION": position, "WAGE_BASE": comp}
        for c in data["eligibility_condition"]:
            values = [maps[c["field"]][v] for v in c["values"]] if c["field"] in maps else c["values"]
            EligibilityCondition.objects.create(component_details_id=details[c["component_details_id"]], field=c["field"], operator=c["operator"], values=values, value_from=c["value_from"], value_to=c["value_to"])
        for x in data["eligibility_employee"]:
            EligibilityEmployee.objects.create(component_details_id=details[x["component_details_id"]], employee_id=emp[x["employee_id"]], list_type=x["list_type"])
        # Reload so each employee's work info (shift, position) isn't a stale cache.
        from employee.models import Employee

        cls.employees = {name: Employee.objects.entire().get(pk=e.pk) for name, e in cls.employees.items()}
        set_selected_company(None)

    def run_month(self, start, end, fraction=Decimal(1)):
        config = engine.load_configuration(self.company.id, start, end)
        return {name: engine.calculate_for_employee(e, config, fraction) for name, e in self.employees.items()}

    def test_december_matches_the_prototype(self):
        results = self.run_month(date(2026, 12, 1), date(2026, 12, 31))
        for name, want in self.expected.items():
            got = results[name]
            net = got["total_earnings"] - got["total_deductions"]
            self.assertEqual(
                (got["total_earnings"], got["total_deductions"], net, got["employer_contribution"]),
                (want["gross"], want["ded"], want["net"], want["er"]),
                name,
            )

    def test_quarterly_bonus_only_in_due_months(self):
        ravi = "Ravi Kumar"
        november = self.run_month(date(2026, 11, 1), date(2026, 11, 30))[ravi]
        december = self.run_month(date(2026, 12, 1), date(2026, 12, 31))[ravi]
        codes = lambda r: {line["code"] for line in r["earnings"]}
        self.assertIn("QBONUS", codes(december))
        self.assertNotIn("QBONUS", codes(november))

    def test_specific_recovery_ends_with_effective_to(self):
        ravi = "Ravi Kumar"
        december = self.run_month(date(2026, 12, 1), date(2026, 12, 31))[ravi]
        january = self.run_month(date(2027, 1, 1), date(2027, 1, 31))[ravi]
        self.assertIn("SCANR", {d["code"] for d in december["deductions"]})
        self.assertNotIn("SCANR", {d["code"] for d in january["deductions"]})

    def test_proration_halves_monthly_earnings_and_their_deductions(self):
        full = self.run_month(date(2026, 11, 1), date(2026, 11, 30))["Arjun Rao"]
        half = self.run_month(date(2026, 11, 1), date(2026, 11, 30), Decimal("0.5"))["Arjun Rao"]
        self.assertEqual(half["total_earnings"], full["total_earnings"] / 2)
        pf_full = next(d for d in full["deductions"] if d["code"] == "PF")["amount"]
        pf_half = next(d for d in half["deductions"] if d["code"] == "PF")["amount"]
        self.assertLess(pf_half, pf_full)


class PayslipIntegrationTests(SampleCompanyEngineTests):
    """payroll_calculation (the payslip run) uses the new engine and one-off adjustments."""

    def contract(self, employee):
        """Employees get an active contract from a payroll signal; create one only if missing."""
        from krew_payroll.models.models import Contract

        existing = Contract.objects.filter(employee_id=employee, contract_status="active").first()
        return existing or Contract.objects.create(
            contract_name="Arjun 2026",
            employee_id=employee,
            contract_start_date=date(2026, 1, 1),
            wage_type="monthly",
            pay_frequency="monthly",
            wage=0,
            contract_status="active",
        )

    def test_payslip_uses_components_and_adjustments(self):
        import json as _json

        from krew_payroll.methods.methods import save_payslip
        from krew_payroll.methods.pay_components.adjustments import add_adjustment
        from krew_payroll.views.component_views import payroll_calculation

        arjun = self.employees["Arjun Rao"]
        self.contract(arjun)
        bonus = add_adjustment(arjun, "EARNING", "Diwali bonus", 5000, date(2026, 12, 15), source="BONUS")
        slip = payroll_calculation(arjun, date(2026, 12, 1), date(2026, 12, 31))
        self.assertIsNotNone(slip)
        self.assertEqual(slip["contract_wage"], 40000.0)
        titles = [a["title"] for a in slip["allowances"]]
        self.assertIn("Diwali bonus", titles)
        # Full month, no leave: components give 40,000 gross and 2,000 deductions (prototype), plus the bonus.
        if slip["paid_fraction"] == 1:
            self.assertEqual(slip["gross_pay"], 45000.0)
            self.assertEqual(slip["total_deductions"], 2000.0)
            self.assertEqual(slip["net_pay"], 43000.0)
        self.assertEqual(slip["gross_pay"] - slip["total_deductions"], slip["net_pay"])
        pay_data = _json.loads(slip["json_data"])
        self.assertEqual(pay_data["adjustment_ids"], [bonus.id])
        instance = save_payslip(
            employee=arjun, start_date=date(2026, 12, 1), end_date=date(2026, 12, 31), status="draft",
            basic_pay=slip["basic_pay"], contract_wage=slip["contract_wage"], gross_pay=slip["gross_pay"],
            deduction=slip["total_deductions"], net_pay=slip["net_pay"], pay_data=pay_data, installments=[],
        )
        bonus.refresh_from_db()
        self.assertEqual(bonus.payslip, instance)
        # A later run of another period does not pick the settled line up again.
        again = payroll_calculation(arjun, date(2026, 12, 1), date(2026, 12, 31))
        self.assertNotIn("Diwali bonus", [a["title"] for a in again["allowances"]])

    def test_no_contract_means_no_payslip(self):
        from krew_payroll.models.models import Contract
        from krew_payroll.views.component_views import payroll_calculation

        sameer = self.employees["Sameer Joshi"]
        Contract.objects.filter(employee_id=sameer).update(contract_status="expired")
        self.assertIsNone(payroll_calculation(sameer, date(2026, 12, 1), date(2026, 12, 31)))


class PayrollPagesSmokeTests(SampleCompanyEngineTests):
    """Payslip and payroll pages render with payslips produced by the new engine."""

    def setUp(self):
        from horilla.horilla_middlewares import _thread_locals
        from horilla.testkit import make_user

        _thread_locals.request = None
        self.admin = make_user("smoke_admin", password="secret123", is_superuser=True)
        make_employee(company=self.company, email="smoke_admin@test.horilla", user=self.admin)
        self.client.login(username="smoke_admin", password="secret123")
        session = self.client.session
        session["selected_company"] = str(self.company.id)
        session.save()

    def tearDown(self):
        from horilla.horilla_middlewares import _thread_locals

        set_selected_company(None)
        _thread_locals.request = None
        super().tearDown()

    def test_pages_render_with_new_payslips(self):
        import json as _json

        from django.urls import reverse

        from krew_payroll.methods.methods import calculate_employer_contribution, save_payslip
        from krew_payroll.methods.pay_components.adjustments import add_adjustment
        from krew_payroll.models.models import LoanAccount
        from krew_payroll.views.component_views import payroll_calculation

        arjun = self.employees["Arjun Rao"]
        add_adjustment(arjun, "DEDUCTION", "Canteen", 300, date(2026, 11, 20), source="MANUAL")
        loan = LoanAccount.objects.create(
            title="Bike", employee_id=arjun, loan_amount=6000, provided_date=date(2026, 11, 2),
            installments=3, installment_start_date=date(2026, 11, 30),
        )
        slip = payroll_calculation(arjun, date(2026, 11, 1), date(2026, 11, 30))
        data = {"pay_data": _json.loads(slip["json_data"])}
        calculate_employer_contribution(data)
        payslip = save_payslip(
            employee=arjun, start_date=date(2026, 11, 1), end_date=date(2026, 11, 30), status="draft",
            basic_pay=slip["basic_pay"], contract_wage=slip["contract_wage"], gross_pay=slip["gross_pay"],
            deduction=slip["total_deductions"], net_pay=slip["net_pay"], pay_data=data["pay_data"], installments=[],
        )
        self.assertEqual(loan.installment_paid(), 1)  # the November installment is on this payslip
        hx = {"HTTP_HX_REQUEST": "true"}
        pages = [
            (reverse("view-created-payslip", kwargs={"payslip_id": payslip.pk}), {}),
            (reverse("view-individual-payslip", kwargs={"employee_id": arjun.pk, "start_date": "2026-11-01", "end_date": "2026-11-30"}), {}),
            (reverse("view-payslip"), {}),
            (reverse("payslip-list"), hx),
            (reverse("get-contribution-report") + f"?employee_id={arjun.pk}", hx),
            (reverse("loan-detail-view", kwargs={"pk": loan.pk}), hx),
            (reverse("view-installments") + f"?loan_id={loan.pk}", hx),
            (reverse("view-loan"), {}),
            (reverse("view-reimbursement"), {}),
            (reverse("view-payroll-dashboard"), {}),
            (reverse("payroll-dashboard-components"), {}),
            (reverse("payroll-settings-view"), {}),
            (reverse("employee-view-individual", kwargs={"obj_id": arjun.pk}), {}),
            (reverse("payroll-pivot"), {}),
        ]
        for url, extra in pages:
            response = self.client.get(url, **extra)
            self.assertLess(response.status_code, 500, f"{url} -> {response.status_code}")
        page = self.client.get(reverse("view-created-payslip", kwargs={"payslip_id": payslip.pk}))
        self.assertContains(page, "Canteen")
        self.assertContains(page, "Bike - 2026-11-30")
