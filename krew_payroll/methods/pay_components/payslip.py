"""
Payslip figures from earnings / deductions (replaces the Allowance / Deduction
calculation inside ``payroll_calculation``).

Money comes from the employee's pay profile: monthly CTC = ctc_annual / 12. The
active contract still sets the working-day and loss-of-pay rules: the existing
monthly computation is run with the monthly CTC as the wage, and the paid share
of the period becomes the proration fraction for recurring monthly earnings.

The result keeps the payslip's existing shape (``allowances`` plus the six
deduction lists, each item with ``title`` / ``amount``), so the payslip page,
PDF, exports, reports and filters work unchanged:

* contract_wage = monthly CTC
* basic_pay     = total earnings for the full period, before loss of pay
* gross_pay     = earnings after proration + one-off earning adjustments
* loss_of_pay   = basic_pay - prorated earnings (shown for information; it is
                  already out of gross, so it is not deducted again)
"""

import json
from decimal import Decimal

from krew_payroll.methods.pay_components import engine
from krew_payroll.methods.pay_components.adjustments import adjustment_lines, adjustments_for

ZERO = Decimal(0)


def _money(value):
    return float(Decimal(value or 0).quantize(Decimal("0.01")))


def paid_fraction(employee, start_date, end_date, ctc_monthly):
    """
    Paid share of the period from the existing attendance / leave rules,
    measured in months of CTC (1 = a full month paid). Returns
    (fraction, details) where details carries paid / unpaid days for display.
    """
    from krew_payroll.methods.methods import monthly_computation

    wage = float(ctc_monthly) if ctc_monthly else 1.0
    details = monthly_computation(employee, wage, start_date, end_date)
    contract = details["contract"]
    period_pay = Decimal(str(details["basic_pay"]))
    lop = Decimal(str(details["loss_of_pay"]))
    # monthly_computation already took loss of pay out of basic_pay when the
    # contract says so; otherwise take it out here.
    paid = period_pay if contract and contract.deduct_leave_from_basic_pay else period_pay - lop
    fraction = max(ZERO, paid / Decimal(str(wage)))
    return fraction, details


def build_payslip(employee, start_date, end_date, config=None):
    """
    The payslip dict for one employee and period, or None when the employee has
    no active contract. ``config`` (from ``engine.load_configuration``) can be
    shared across employees of the same company in one run.
    """
    from employee.models import Employee
    from krew_payroll.models.models import Contract

    from datetime import date as _date

    if isinstance(start_date, str):  # some callers pass ISO strings
        start_date = _date.fromisoformat(start_date)
    if isinstance(end_date, str):
        end_date = _date.fromisoformat(end_date)
    if not isinstance(employee, Employee):  # some callers pass the id
        employee = Employee.objects.entire().filter(pk=employee).first()
        if employee is None:
            return None
    contract = Contract.objects.filter(employee_id=employee, contract_status="active").first()
    if contract is None:
        return None
    worker = engine.worker_for(employee)
    ctc_monthly = worker.ctc_monthly
    fraction, period = paid_fraction(employee, start_date, end_date, ctc_monthly)
    company_id = getattr(getattr(employee, "employee_work_info", None), "company_id_id", None)
    config = config or engine.load_configuration(company_id, start_date, end_date)
    result = engine.calculate_for_employee(employee, config, fraction, worker=worker)

    adjustments = adjustments_for(employee, start_date, end_date)
    extra_earnings, extra_deductions = adjustment_lines(adjustments)

    allowances = [
        {
            "pay_component_id": line["pay_component_id"],
            "code": line["code"],
            "title": line["title"],
            "is_taxable": line["is_taxable"],
            "amount": _money(line["amount"]),
            "exempt_amount": _money(line["exempt_amount"]),
        }
        for line in result["earnings"]
        if line["show_on_payslip"] or line["amount"]
    ] + [
        {
            "adjustment_id": line["adjustment_id"],
            "title": line["title"],
            "is_taxable": line["is_taxable"],
            "amount": _money(line["amount"]),
            "exempt_amount": _money(line["exempt_amount"]),
        }
        for line in extra_earnings
    ]

    def deduction_item(line, key):
        item = {
            key: line[key],
            "title": line["title"],
            "is_pretax": line["is_pretax"],
            "amount": _money(line["amount"]),
            "employer_contribution_rate": 0,
            "employer_contribution_amount": _money(line["employer_contribution_amount"]),
        }
        if "code" in line:
            item["code"] = line["code"]
        return item

    pretax, post_tax = [], []
    for line in result["deductions"]:
        (pretax if line["is_pretax"] else post_tax).append(deduction_item(line, "pay_component_id"))
    for line in extra_deductions:
        (pretax if line["is_pretax"] else post_tax).append(deduction_item(line, "adjustment_id"))

    full_earnings = result["full_month_earnings"]
    prorated_earnings = result["total_earnings"]
    gross = prorated_earnings + sum((l["amount"] for l in extra_earnings), ZERO)
    total_deductions = result["total_deductions"] + sum((l["amount"] for l in extra_deductions), ZERO)
    exempt = result["tax_exempt"] + sum((l["exempt_amount"] for l in extra_earnings), ZERO)
    pretax_total = sum((Decimal(str(d["amount"])) for d in pretax), ZERO)
    taxable_gross = max(ZERO, gross - exempt - pretax_total)
    net = gross - total_deductions

    payslip = {
        "employee": employee,
        "contract_wage": _money(ctc_monthly),
        "basic_pay": _money(full_earnings),
        "gross_pay": _money(gross),
        "taxable_gross_pay": _money(taxable_gross),
        "net_pay": _money(net),
        "allowances": allowances,
        "paid_days": period.get("paid_days", 0),
        "unpaid_days": period.get("unpaid_days", 0),
        "partial_pay_days": period.get("partial_pay_days", 0),
        "basic_pay_deductions": [],
        "gross_pay_deductions": [],
        "pretax_deductions": pretax,
        "post_tax_deductions": post_tax,
        "tax_deductions": [],
        "net_deductions": [],
        "total_deductions": _money(total_deductions),
        "loss_of_pay": _money(max(ZERO, full_earnings - prorated_earnings)),
        "custom_leave_deduction": 0.0,
        "custom_leave_breakdown": period.get("custom_leave_breakdown", []),
        "federal_tax": 0.0,
        "employer_contribution": _money(result["employer_contribution"]),
        "paid_fraction": float(round(fraction, 6)),
        "held_for_review": result["held"],
        "start_date": start_date,
        "end_date": end_date,
        "range": f"{start_date.strftime('%b %d %Y')} - {end_date.strftime('%b %d %Y')}",
    }
    payslip["adjustment_ids"] = [adj.id for adj in adjustments]
    data_to_json = payslip.copy()
    data_to_json["employee"] = employee.id
    data_to_json["start_date"] = start_date.strftime("%Y-%m-%d")
    data_to_json["end_date"] = end_date.strftime("%Y-%m-%d")
    payslip["json_data"] = json.dumps(data_to_json)
    # Old payslips linked Deduction installments; new ones link adjustment lines.
    payslip["installments"] = []
    return payslip
