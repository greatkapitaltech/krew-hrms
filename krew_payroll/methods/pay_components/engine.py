"""
Pay-run engine for earnings and deductions.

For one employee and one pay period:
1. Each active component's PUBLISHED version covering the period is used. A
   version can start on any day; when two overlap a period, the newest one is
   used for the whole period (no split by days).
2. Frequency: recurring components run when due (quarterly / half-yearly /
   annual in financial-year months), one-time ones when their date falls in
   the period. On-demand and exit-triggered ones are not part of a regular run.
3. Eligibility: Include All, Condition-Based (rules ANDed) or Specific employees;
   the exclude list removes named workers in the other modes.
4. Earnings are calculated first, in depends_on order, then deductions (which
   may use the earnings). Every figure is rounded to the nearest rupee (₹0.50
   rounds up), then the min / max limits apply.
5. Recurring every-cycle / monthly earnings are prorated by the paid fraction
   (attendance and loss of pay); deductions are calculated on the prorated
   earnings. The employer share is calculated on the same base and is not
   limited by min / max.

Load the configuration once per pay run with ``load_configuration`` and pass it
to ``calculate_for_employee`` for every employee.
"""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from django.db.models import Prefetch

from krew_payroll.methods.pay_components.formula import FormulaError, evaluate, parse
from krew_payroll.models.pay_components import (
    LIST_CONDITION_FIELDS,
    Calculation,
    CalculationMode,
    CalculationPurpose,
    ComponentType,
    ConditionField,
    EligibilityMode,
    ExemptionType,
    Frequency,
    ListType,
    PayComponent,
    PayComponentDetails,
    PublishState,
    RecurringInterval,
    TaxTreatment,
)

ZERO = Decimal(0)
# Financial-year months when a periodic component is due.
DUE_MONTHS = {
    RecurringInterval.QUARTERLY: (6, 9, 12, 3),
    RecurringInterval.HALF_YEARLY: (9, 3),
    RecurringInterval.ANNUAL: (3,),
}
PRORATED_INTERVALS = (RecurringInterval.EVERY_CYCLE, RecurringInterval.MONTHLY)


def round_rupee(value):
    return Decimal(value).quantize(Decimal(1), rounding=ROUND_HALF_UP)


# ------------------------------------------------------------------ worker attributes


@dataclass
class Worker:
    employee_id: int
    ctc_annual: Decimal = ZERO
    worker_class_id: int = None
    employment_type: str = None
    grade_id: int = None
    work_state_id: int = None
    work_site_id: int = None
    shift_id: int = None
    job_position_id: int = None

    @property
    def ctc_monthly(self):
        return Decimal(self.ctc_annual or 0) / 12

    def attribute(self, field):
        return {
            ConditionField.SHIFT: self.shift_id,
            ConditionField.STATE: self.work_state_id,
            ConditionField.SITE: self.work_site_id,
            ConditionField.GRADE: self.grade_id,
            ConditionField.DESIGNATION: self.job_position_id,
            ConditionField.WORKER_CLASS: self.worker_class_id,
            ConditionField.EMPLOYMENT_TYPE: self.employment_type,
        }.get(field)


def worker_for(employee, profile=False):
    """Read the employee's pay profile and work info into a Worker."""
    from krew_payroll.models.pay_components import EmployeePayProfile

    if profile is False:  # not passed in: look it up
        profile = EmployeePayProfile.objects.entire().filter(employee=employee).first()
    work_info = getattr(employee, "employee_work_info", None)
    return Worker(
        employee_id=employee.pk,
        ctc_annual=Decimal(profile.ctc_annual) if profile else ZERO,
        worker_class_id=profile.worker_class_id if profile else None,
        employment_type=profile.employment_type if profile else None,
        grade_id=profile.grade_id if profile else None,
        work_state_id=profile.work_state_id if profile else None,
        work_site_id=profile.work_site_id if profile else None,
        shift_id=getattr(work_info, "shift_id_id", None),
        job_position_id=getattr(work_info, "job_position_id_id", None),
    )


def workers_for(employees):
    """Workers for many employees with one pay-profile query: {employee_id: Worker}."""
    from krew_payroll.models.pay_components import EmployeePayProfile

    employees = list(employees)
    profiles = {
        p.employee_id: p
        for p in EmployeePayProfile.objects.entire().filter(employee__in=employees)
    }
    return {e.pk: worker_for(e, profiles.get(e.pk)) for e in employees}


# ------------------------------------------------------------------ configuration


@dataclass
class ComponentRun:
    component: PayComponent
    details: PayComponentDetails
    calcs: dict
    conditions: list
    include: set
    exclude: set


def _live_version(versions, start, end):
    live = [
        v
        for v in versions
        if v.publish_state == PublishState.PUBLISHED
        and v.effective_from
        and v.effective_from <= end
        and (v.effective_to is None or v.effective_to >= start)
    ]
    return live[-1] if live else None


def _order_by_depends(runs):
    by_id = {r.component.id: r for r in runs}
    indegree = {cid: 0 for cid in by_id}
    after = {cid: [] for cid in by_id}
    for run in runs:
        for dep in run.details.depends_on or []:
            if dep in by_id and dep != run.component.id:
                indegree[run.component.id] += 1
                after[dep].append(run.component.id)
    queue = sorted(
        (r for r in runs if not indegree[r.component.id]),
        key=lambda r: (r.details.display_order, r.component.id),
    )
    ordered = []
    while queue:
        run = queue.pop(0)
        ordered.append(run)
        for nxt in after[run.component.id]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(by_id[nxt])
    ordered += [r for r in runs if r not in ordered]  # a cycle can't be published, but never drop one
    return ordered


def load_configuration(company_id, start, end):
    """Every active component's live version for the period, ready to calculate."""
    components = (
        PayComponent.objects.entire()
        .filter(company_id=company_id, is_active=True)
        .prefetch_related(
            Prefetch(
                "versions",
                queryset=PayComponentDetails.objects.entire()
                .filter(publish_state=PublishState.PUBLISHED)
                .order_by("version_no")
                .prefetch_related(
                    Prefetch(
                        "calculations",
                        queryset=Calculation.objects.entire().prefetch_related("slabs"),
                    ),
                    "conditions",
                    "employee_lists",
                ),
            )
        )
    )
    earnings, deductions = [], []
    for component in components:
        details = _live_version(list(component.versions.all()), start, end)
        if not details:
            continue
        run = ComponentRun(
            component=component,
            details=details,
            calcs={c.purpose: c for c in details.calculations.all()},
            conditions=list(details.conditions.all()),
            include={e.employee_id for e in details.employee_lists.all() if e.list_type == ListType.INCLUDE},
            exclude={e.employee_id for e in details.employee_lists.all() if e.list_type == ListType.EXCLUDE},
        )
        (earnings if component.type == ComponentType.EARNING else deductions).append(run)
    codes = dict(
        PayComponent.objects.entire().filter(company_id=company_id).values_list("id", "code")
    )
    return {
        "earnings": _order_by_depends(earnings),
        "deductions": sorted(deductions, key=lambda r: (r.details.display_order, r.component.id)),
        "codes": codes,
        "start": start,
        "end": end,
    }


# ------------------------------------------------------------------ checks


def _number_ok(operator, value, low, high):
    value, low = Decimal(value or 0), Decimal(low or 0)
    if operator == "EQ":
        return value == low
    if operator == "GT":
        return value > low
    if operator == "LT":
        return value < low
    if operator == "BETWEEN":
        return low <= value <= Decimal(high or 0)
    return False


def is_due(details, start, end):
    """Frequency check for a regular pay run of [start, end]."""
    if details.frequency == Frequency.RECURRING:
        months = DUE_MONTHS.get(details.recurring_interval)
        return months is None or end.month in months
    if details.frequency == Frequency.ONE_TIME:
        return bool(details.one_time_date and start <= details.one_time_date <= end)
    return False  # ON_DEMAND / EXIT_TRIGGERED are not part of a regular run


def is_eligible(run, worker, values, codes):
    details = run.details
    if details.eligibility_mode == EligibilityMode.SPECIFIC:
        return worker.employee_id in run.include
    if worker.employee_id in run.exclude:
        return False
    if details.eligibility_mode == EligibilityMode.ALL:
        return True
    if not run.conditions:
        return False
    return all(condition_matches(cond, worker, values, codes) for cond in run.conditions)


def condition_matches(cond, worker, values=None, codes=None):
    """One eligibility rule (ANDed with the others). WAGE_BASE needs this run's earnings."""
    if cond.field in [f.value for f in LIST_CONDITION_FIELDS]:
        return worker.attribute(cond.field) in (cond.values or [])
    if cond.field == ConditionField.CTC:
        return _number_ok(cond.operator, worker.ctc_annual, cond.value_from, cond.value_to)
    if cond.field == ConditionField.WAGE_BASE:
        total = sum((values or {}).get((codes or {}).get(i), ZERO) for i in cond.values or [])
        return _number_ok(cond.operator, total, cond.value_from, cond.value_to)
    return True


# ------------------------------------------------------------------ amounts


def _base_sum(calc, values, codes, worker):
    total = sum(values.get(codes.get(i), ZERO) for i in calc.base_component_ids or [])
    if calc.base_includes_ctc:
        total += worker.ctc_monthly
    return Decimal(total)


def _formula_value(calc, values, worker, allow_ctc):
    env = dict(values)
    if allow_ctc:
        env["CTC"] = worker.ctc_monthly
    try:
        return evaluate(parse(calc.formula), env)
    except FormulaError:
        return ZERO


def _simple_value(calc, values, codes, worker, allow_ctc):
    if calc.mode == CalculationMode.FLAT:
        return Decimal(calc.flat_amount or 0)
    if calc.mode == CalculationMode.PERCENTAGE:
        return _base_sum(calc, values, codes, worker) * Decimal(calc.rate or 0) / 100
    if calc.mode == CalculationMode.FORMULA:
        return _formula_value(calc, values, worker, allow_ctc)
    return ZERO


def amount_for(run, worker, values, codes, pay_month):
    """
    One component for one worker. Returns a dict with ``amount`` (None when it
    produces no figure), plus ``exempt``, ``employer`` and ``held`` where relevant.
    """
    details = run.details
    calc = run.calcs.get(CalculationPurpose.COMPONENT_VALUE)
    is_earning = run.component.type == ComponentType.EARNING
    if not calc:
        return {"amount": None}
    base = None
    if calc.mode == CalculationMode.FLAT:
        amount = Decimal(calc.flat_amount or 0)
    elif calc.mode == CalculationMode.PERCENTAGE:
        base = _base_sum(calc, values, codes, worker)
        amount = base * Decimal(calc.rate or 0) / 100
    elif calc.mode == CalculationMode.FORMULA:
        amount = _formula_value(calc, values, worker, is_earning)
    elif calc.mode == CalculationMode.SLAB:
        base = _base_sum(calc, values, codes, worker)
        slabs = list(calc.slabs.all())

        def in_range(slab):
            return base >= slab.range_from and (slab.range_to is None or base <= slab.range_to)

        hit = next((s for s in slabs if in_range(s) and s.applies_in_month == pay_month), None) or next(
            (s for s in slabs if in_range(s) and s.applies_in_month is None), None
        )
        if hit is None:
            return {"amount": None, "held": True}
        amount = Decimal(hit.amount)
    else:  # SYSTEM_COMPUTED: produced by the TDS engine
        return {"amount": None}

    amount = round_rupee(amount)
    if details.max_amount is not None and amount > details.max_amount:
        amount = Decimal(details.max_amount)
    if details.min_amount is not None and amount < details.min_amount:
        amount = Decimal(details.min_amount)
    out = {"amount": amount}

    if is_earning and details.tax_treatment == TaxTreatment.TAX_FREE:
        limit = run.calcs.get(CalculationPurpose.EXEMPTION_LIMIT)
        if details.exemption_type == ExemptionType.UP_TO_LIMIT and limit:
            out["exempt"] = min(
                amount, max(ZERO, round_rupee(_simple_value(limit, values, codes, worker, True)))
            )
        else:
            out["exempt"] = amount

    employer = run.calcs.get(CalculationPurpose.EMPLOYER_SHARE)
    if employer:
        if employer.mode == CalculationMode.FLAT:
            out["employer"] = round_rupee(Decimal(employer.flat_amount or 0))
        else:
            share_base = base if base is not None else _base_sum(calc, values, codes, worker)
            out["employer"] = round_rupee(share_base * Decimal(employer.rate or 0) / 100)
    return out


def _is_prorated(details):
    return details.frequency == Frequency.RECURRING and details.recurring_interval in PRORATED_INTERVALS


def calculate_for_employee(employee, config, paid_fraction=Decimal(1), worker=None):
    """
    Earnings and deductions for one employee in the configured period.

    ``paid_fraction`` is the paid share of the period (1 = full month,
    0.5 = half the month unpaid, 2 = two full months). Returns lines in the
    payslip's shape plus totals; ``held`` is True when a slab had no row.
    """
    worker = worker or worker_for(employee)
    codes, start, end = config["codes"], config["start"], config["end"]
    pay_month = end.month
    fraction = Decimal(paid_fraction)

    full_values, earnings = {}, []
    for run in config["earnings"]:
        details = run.details
        if not is_due(details, start, end) or not is_eligible(run, worker, full_values, codes):
            continue
        result = amount_for(run, worker, full_values, codes, pay_month)
        if result["amount"] is None:
            continue
        full_values[run.component.code] = result["amount"]
        amount, exempt = result["amount"], result.get("exempt")
        if _is_prorated(details) and fraction != 1:
            amount = round_rupee(amount * fraction)
            exempt = min(amount, round_rupee(exempt * fraction)) if exempt is not None else None
        earnings.append(
            {
                "pay_component_id": run.component.id,
                "code": run.component.code,
                "title": details.payslip_label or run.component.name,
                "amount": amount,
                "is_taxable": details.tax_treatment != TaxTreatment.TAX_FREE,
                "exempt_amount": exempt or ZERO,
                "show_on_payslip": details.show_on_payslip,
                "display_order": details.display_order,
            }
        )

    paid_values = {line["code"]: line["amount"] for line in earnings}
    deductions, held = [], False
    for run in config["deductions"]:
        details = run.details
        if not is_due(details, start, end) or not is_eligible(run, worker, paid_values, codes):
            continue
        result = amount_for(run, worker, paid_values, codes, pay_month)
        if result.get("held"):
            held = True
        if result["amount"] is None:
            continue
        deductions.append(
            {
                "pay_component_id": run.component.id,
                "code": run.component.code,
                "title": details.payslip_label or run.component.name,
                "amount": result["amount"],
                "is_pretax": bool(details.reduces_taxable_income),
                "employer_contribution_amount": result.get("employer", ZERO),
                "show_on_payslip": details.show_on_payslip,
                "display_order": details.display_order,
            }
        )

    earnings.sort(key=lambda line: (line["display_order"], line["pay_component_id"]))
    total_earnings = sum((line["amount"] for line in earnings), ZERO)
    total_deductions = sum((line["amount"] for line in deductions), ZERO)
    return {
        "earnings": earnings,
        "deductions": deductions,
        "full_month_earnings": sum(full_values.values(), ZERO),
        "total_earnings": total_earnings,
        "total_deductions": total_deductions,
        "employer_contribution": sum(
            (line["employer_contribution_amount"] for line in deductions), ZERO
        ),
        "tax_exempt": sum((line["exempt_amount"] for line in earnings), ZERO),
        "held": held,
    }
