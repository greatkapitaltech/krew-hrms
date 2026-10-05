"""
One-off employee lines (EmployeePayAdjustment): loan disbursement and
installments, reimbursement, leave encashment, bonus, penalty and manual
payslip additions. The pay run adds the lines dated inside the period.
"""

from decimal import Decimal

from krew_payroll.models.pay_components import AdjustmentSource, ComponentType, EmployeePayAdjustment


def add_adjustment(employee, type_, title, amount, pay_date, source=AdjustmentSource.MANUAL, source_id=None, **flags):
    """Create one adjustment line. ``flags``: is_taxable / reduces_taxable_income."""
    return EmployeePayAdjustment.objects.create(
        employee=employee,
        type=type_,
        title=title,
        amount=Decimal(str(amount)),
        pay_date=pay_date,
        source=source,
        source_id=source_id,
        **flags,
    )


def remove_adjustments(source, source_id, unsettled_only=True):
    """Delete the lines a source created (e.g. a rejected reimbursement); settled lines stay."""
    lines = EmployeePayAdjustment.objects.entire().filter(source=source, source_id=source_id)
    if unsettled_only:
        lines = lines.filter(payslip__isnull=True)
    lines.delete()


def adjustments_for(employee, start, end):
    """The employee's adjustment lines dated inside [start, end], not yet on another payslip."""
    return list(
        EmployeePayAdjustment.objects.entire()
        .filter(employee=employee, pay_date__gte=start, pay_date__lte=end, payslip__isnull=True)
        .order_by("pay_date", "id")
    )


def adjustment_lines(adjustments):
    """Split adjustments into payslip earning / deduction lines."""
    earnings, deductions = [], []
    for adj in adjustments:
        if adj.type == ComponentType.EARNING:
            earnings.append(
                {
                    "adjustment_id": adj.id,
                    "title": adj.title,
                    "amount": Decimal(adj.amount),
                    "is_taxable": adj.is_taxable,
                    "exempt_amount": Decimal(0) if adj.is_taxable else Decimal(adj.amount),
                    "source": adj.source,
                }
            )
        else:
            deductions.append(
                {
                    "adjustment_id": adj.id,
                    "title": adj.title,
                    "amount": Decimal(adj.amount),
                    "is_pretax": adj.reduces_taxable_income,
                    "employer_contribution_amount": Decimal(0),
                    "source": adj.source,
                }
            )
    return earnings, deductions
