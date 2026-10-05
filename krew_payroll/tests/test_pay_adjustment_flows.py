"""One-off payslip lines: loans, reimbursements, penalties, bonus and manual deductions."""

from datetime import date
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, TestCase

from base.models import PenaltyAccounts
from horilla.horilla_middlewares import _thread_locals, set_selected_company
from horilla.testkit import make_company, make_employee, make_user
from krew_payroll.models.models import LoanAccount, Payslip, Reimbursement
from krew_payroll.models.pay_components import EmployeePayAdjustment


class AdjustmentFlowTests(TestCase):
    def setUp(self):
        self.company = make_company("Sarvam Flows")
        self.user = make_user("flows_admin", password="secret123", is_superuser=True)
        self.emp = make_employee(company=self.company, email="flows@test.horilla", user=self.user)
        request = RequestFactory().get("/")
        request.user = self.user
        request.session = {}
        _thread_locals.request = request

    def tearDown(self):
        _thread_locals.request = None
        set_selected_company(None)
        super().tearDown()

    def lines(self, source):
        return EmployeePayAdjustment.objects.entire().filter(source=source).order_by("pay_date", "id")

    def loan(self, **extra):
        data = dict(
            title="Bike loan", employee_id=self.emp, type="loan", loan_amount=30000,
            provided_date=date(2026, 10, 5), installments=3, installment_start_date=date(2026, 11, 30),
        )
        data.update(extra)
        return LoanAccount.objects.create(**data)

    def test_loan_creates_disbursement_and_installments(self):
        loan = self.loan()
        earn = self.lines("LOAN").filter(type="EARNING")
        self.assertEqual([(e.amount, e.pay_date, e.is_taxable) for e in earn], [(Decimal("30000.00"), date(2026, 10, 5), False)])
        inst = loan.installment_lines()
        self.assertEqual(inst.count(), 3)
        self.assertEqual(sum(i.amount for i in inst), Decimal("30000.00"))
        self.assertEqual(loan.installment_paid(), 0)
        # Editing the schedule before anything is paid rebuilds it.
        loan.installments = 2
        loan.save()
        self.assertEqual(loan.installment_lines().count(), 2)

    def test_paid_installments_are_kept_and_counted(self):
        loan = self.loan()
        first = loan.installment_lines().first()
        slip = Payslip.objects.create(
            employee_id=self.emp, start_date=date(2026, 11, 1), end_date=date(2026, 11, 30),
            contract_wage=0, basic_pay=0, gross_pay=0, deduction=0, net_pay=0, pay_head_data={},
        )
        first.payslip = slip
        first.save()
        self.assertEqual(loan.installment_paid(), 1)
        self.assertTrue(loan.has_paid_installments())
        loan.settled = True
        loan.save()
        self.assertEqual(list(loan.installment_lines()), [first])

    def test_fine_has_no_disbursement(self):
        fine = self.loan(type="fine", title="Damaged scanner")
        self.assertFalse(self.lines("LOAN").filter(type="EARNING").exists())
        self.assertEqual(fine.installment_lines().count(), 3)

    def test_reimbursement_approve_and_reject(self):
        reimb = Reimbursement.objects.create(
            title="Travel", type="reimbursement", employee_id=self.emp, allowance_on=date(2026, 12, 10),
            amount=1500, attachment=SimpleUploadedFile("bill.pdf", b"%PDF-1.4"),
        )
        self.assertFalse(self.lines("REIMBURSEMENT").exists())
        reimb.status = "approved"
        reimb.save()
        line = self.lines("REIMBURSEMENT").get()
        self.assertEqual((line.amount, line.is_taxable, line.pay_date), (Decimal("1500.00"), False, date(2026, 12, 10)))
        reimb.save()  # saving again doesn't duplicate the line
        self.assertEqual(self.lines("REIMBURSEMENT").count(), 1)
        reimb.status = "rejected"
        reimb.save()
        self.assertFalse(self.lines("REIMBURSEMENT").exists())

    def test_penalty_line_follows_the_penalty(self):
        penalty = PenaltyAccounts.objects.create(employee_id=self.emp, penalty_amount=250)
        line = self.lines("PENALTY").get()
        self.assertEqual((line.type, line.amount, line.source_id), ("DEDUCTION", Decimal("250.00"), penalty.pk))
        penalty.delete()
        self.assertFalse(self.lines("PENALTY").exists())

    def test_bonus_form_creates_an_earning_line(self):
        from krew_payroll.forms.component_forms import BonusForm

        form = BonusForm({"title": "Diwali bonus", "date": "2026-11-10", "employee_id": self.emp.id, "amount": "5000", "is_taxable": "on"})
        self.assertTrue(form.is_valid(), form.errors)
        line = form.save()
        self.assertEqual((line.type, line.source, line.amount, line.is_taxable), ("EARNING", "BONUS", Decimal("5000.00"), True))
