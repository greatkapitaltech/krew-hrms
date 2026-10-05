"""
Rules for earnings and deductions: what each type allows, code generation,
dependencies, and the publish checks.

Nothing here depends on a component's name or code: every rule is generic.
"""

import re
from datetime import date

from django.apps import apps
from django.utils import timezone

from krew_payroll.methods.pay_components.formula import FormulaError, parse, references
from krew_payroll.models.pay_components import (
    LIST_CONDITION_FIELDS,
    NUMBER_CONDITION_FIELDS,
    CalculationMode,
    CalculationPurpose,
    ComponentType,
    ConditionField,
    EligibilityMode,
    EmploymentType,
    ExemptionType,
    Frequency,
    ListType,
    PayComponent,
    PayComponentDetails,
    PublishState,
    RecurringInterval,
    TaxTreatment,
)

# What differs between earnings and deductions. Nothing depends on a component's name.
KIND = {
    ComponentType.EARNING: {
        "modes": [CalculationMode.FLAT, CalculationMode.PERCENTAGE, CalculationMode.FORMULA],
        "intervals": [
            RecurringInterval.EVERY_CYCLE,
            RecurringInterval.QUARTERLY,
            RecurringInterval.HALF_YEARLY,
            RecurringInterval.ANNUAL,
        ],
        "frequencies": [
            Frequency.RECURRING,
            Frequency.ONE_TIME,
            Frequency.ON_DEMAND,
            Frequency.EXIT_TRIGGERED,
        ],
        "ctc_in_formula": True,
        "purposes": [CalculationPurpose.COMPONENT_VALUE, CalculationPurpose.EXEMPTION_LIMIT],
    },
    ComponentType.DEDUCTION: {
        "modes": [
            CalculationMode.FLAT,
            CalculationMode.PERCENTAGE,
            CalculationMode.FORMULA,
            CalculationMode.SLAB,
            CalculationMode.SYSTEM_COMPUTED,
        ],
        "intervals": [
            RecurringInterval.MONTHLY,
            RecurringInterval.QUARTERLY,
            RecurringInterval.HALF_YEARLY,
            RecurringInterval.ANNUAL,
        ],
        "frequencies": [Frequency.RECURRING, Frequency.ONE_TIME, Frequency.EXIT_TRIGGERED],
        "ctc_in_formula": False,
        "purposes": [CalculationPurpose.COMPONENT_VALUE, CalculationPurpose.EMPLOYER_SHARE],
    },
}
EXEMPTION_MODES = [CalculationMode.FLAT, CalculationMode.PERCENTAGE, CalculationMode.FORMULA]
EMPLOYER_MODES = [CalculationMode.FLAT, CalculationMode.PERCENTAGE]
RESERVED_CODES = {"CTC", "MIN", "MAX"}
SECTIONS = ("identity", "frequency", "eligibility", "calculation", "employer", "tax", "publish")


def today():
    return timezone.localdate()


# ------------------------------------------------------------------ months
# Versions change only on month boundaries.


# ------------------------------------------------------------------ lookups


def company_components(company_id, type_=None):
    qs = PayComponent.objects.entire().filter(company_id=company_id)
    return qs.filter(type=type_) if type_ else qs


def component_by_code(company_id, code):
    return company_components(company_id).filter(code=code).first()


def versions_of(component):
    return PayComponentDetails.objects.entire().filter(component=component).order_by(
        "version_no"
    )


def published_of(component):
    return versions_of(component).filter(publish_state=PublishState.PUBLISHED)


def draft_of(component):
    return versions_of(component).filter(publish_state=PublishState.DRAFT).first()


def latest_published(component):
    return published_of(component).last()


def calc_of(details, purpose):
    return details.calculations.filter(purpose=purpose).first()


# ------------------------------------------------------------------ codes


def generate_code(company_id, name):
    """Initials for multi-word names, else the first word; unique per company."""
    words = [w for w in re.split(r"[^A-Z0-9]+", name.upper()) if w]
    base = "".join(w[0] for w in words)[:6] if len(words) >= 2 else (words[0] if words else "COMP")[:6]
    if base in RESERVED_CODES:
        base += "X"
    taken = set(company_components(company_id).values_list("code", flat=True))
    code, n = base, 2
    while code in taken:
        code, n = f"{base}{n}", n + 1
    return code


# ------------------------------------------------------------------ dependencies


def calc_references(calc, company_id):
    """Component ids a calculation uses: its base components plus formula codes."""
    if not calc:
        return set()
    ids = set(calc.base_component_ids or [])
    if calc.mode == CalculationMode.FORMULA and calc.formula:
        try:
            codes = references(parse(calc.formula))
        except FormulaError:
            codes = set()
        codes.discard("CTC")
        ids |= set(
            company_components(company_id).filter(code__in=codes).values_list("id", flat=True)
        )
    return ids


def derive_depends(details):
    """depends_on for a version: everything its value, exemption limit and wage-base rules use."""
    company_id = details.component.company_id_id
    ids = set()
    for calc in details.calculations.exclude(purpose=CalculationPurpose.EMPLOYER_SHARE):
        ids |= calc_references(calc, company_id)
    for cond in details.conditions.filter(field=ConditionField.WAGE_BASE):
        ids |= {int(v) for v in cond.values or []}
    ids.discard(details.component_id)
    return sorted(ids)


def find_cycle(details):
    """Return the codes of a circular reference through this version, e.g. [HRA, SPL, HRA]."""
    component = details.component
    company_id = component.company_id_id
    graph = {}
    for other in company_components(company_id, ComponentType.EARNING):
        if other.id == component.id:
            graph[other.id] = derive_depends(details)
        else:
            live = latest_published(other) or draft_of(other)
            graph[other.id] = list(live.depends_on or []) if live else []
    start, path, seen = component.id, [], set()

    def dfs(node):
        path.append(node)
        for nxt in graph.get(node, []):
            if nxt == start:
                path.append(nxt)
                return True
            if nxt not in seen:
                seen.add(nxt)
                if dfs(nxt):
                    return True
        path.pop()
        return False

    if not dfs(start):
        return None
    codes = dict(company_components(company_id).values_list("id", "code"))
    return [codes.get(i, str(i)) for i in path]


def dependants_of(component):
    """Active components whose live (or draft) version uses this component."""
    out = []
    for other in company_components(component.company_id_id).filter(is_active=True).exclude(
        pk=component.pk
    ):
        live = latest_published(other) or draft_of(other)
        if live and component.id in (live.depends_on or []):
            out.append(other)
    return out


# ------------------------------------------------------------------ condition values


def company_states(company_id):
    """
    Active GST states the company is registered in (Company Setup → Place of
    Work). The only states offered or accepted for that company: the STATE
    condition, work sites and employee pay profiles.
    """
    GSTStateConfig = apps.get_model("krew_company_onboarding", "GSTStateConfig")
    return GSTStateConfig.objects.filter(
        is_active=True, state_registrations__company_id=company_id
    ).distinct().order_by("code")


def condition_choices(field, company_id):
    """Allowed ids (or codes) for a list condition field in this company."""
    from base.models import EmployeeShift, Grade, JobPosition, WorkerClass
    from krew_payroll.models.pay_components import WorkSite

    if field == ConditionField.SHIFT:
        return set(EmployeeShift.objects.entire().values_list("id", flat=True))
    if field == ConditionField.STATE:
        return set(company_states(company_id).values_list("id", flat=True))
    if field == ConditionField.SITE:
        return set(WorkSite.objects.entire().filter(company_id=company_id).values_list("id", flat=True))
    if field == ConditionField.GRADE:
        return set(Grade.objects.entire().filter(company_id=company_id).values_list("id", flat=True))
    if field == ConditionField.DESIGNATION:
        return set(JobPosition.objects.entire().values_list("id", flat=True))
    if field == ConditionField.WORKER_CLASS:
        return set(
            WorkerClass.objects.entire().filter(company_id=company_id).values_list("id", flat=True)
        )
    if field == ConditionField.EMPLOYMENT_TYPE:
        return set(EmploymentType.values)
    return set()


def operators_for(field):
    return ["IN"] if field in LIST_CONDITION_FIELDS else ["EQ", "GT", "LT", "BETWEEN"]


# ------------------------------------------------------------------ publish checks


def _validate_calc(calc, component, where, purpose):
    errors, kind = [], KIND[component.type]
    if not calc:
        return [f"{where}: no calculation"]
    if purpose == CalculationPurpose.COMPONENT_VALUE and calc.mode not in kind["modes"]:
        errors.append(f"{where}: {calc.get_mode_display()} isn't available for {component.type.lower()}s")
    if calc.mode == CalculationMode.FLAT and not (calc.flat_amount and calc.flat_amount > 0):
        errors.append(f"{where}: enter an amount above ₹0")
    if calc.mode == CalculationMode.PERCENTAGE:
        if not (calc.rate and 0 < calc.rate <= 100):
            errors.append(f"{where}: rate must be between 0 and 100")
        if not calc.base_component_ids and not calc.base_includes_ctc:
            extra = " (or CTC)" if kind["ctc_in_formula"] else ""
            errors.append(f"{where}: pick at least one component{extra} to take the percentage of")
    if calc.mode == CalculationMode.FORMULA:
        try:
            codes = references(parse(calc.formula))
        except FormulaError as exc:
            errors.append(f"{where}: {exc}")
            codes = set()
        for code in sorted(codes):
            if code == "CTC" and kind["ctc_in_formula"]:
                continue
            ref = component_by_code(component.company_id_id, code)
            if not ref:
                errors.append(f'{where}: unknown component "{code}"')
            elif ref.type != ComponentType.EARNING:
                errors.append(f'{where}: "{code}" is a deduction; formulas can only use earnings')
    if calc.mode == CalculationMode.SLAB:
        slabs = list(calc.slabs.all().order_by("range_from"))
        if not calc.base_component_ids:
            errors.append(f"{where}: pick the earnings that make up the slab's wage figure")
        if not slabs:
            errors.append(f"{where}: add at least one slab row")
        for i, slab in enumerate(slabs, 1):
            if slab.range_to is not None and slab.range_to < slab.range_from:
                errors.append(f"Slab row {i}: Range To is below Range From")
        regular = [s for s in slabs if s.applies_in_month is None]
        if slabs and (not regular or regular[-1].range_to is not None):
            errors.append(f"{where}: the last row must be open-ended (leave Range To empty)")
    return errors


def validate(details):
    """
    The publish checks. Returns {"errors": {section: [..]}, "warnings": [..], "total": n}.
    A DRAFT may be saved with errors; publishing, or saving a PUBLISHED version, needs total == 0.
    """
    errs = {key: [] for key in SECTIONS}
    warns = []
    component = details.component
    kind = KIND[component.type]

    if not (component.name or "").strip():
        errs["identity"].append("Name is required")
    elif (
        company_components(component.company_id_id, component.type)
        .exclude(pk=component.pk)
        .filter(name__iexact=component.name.strip())
        .exists()
    ):
        errs["identity"].append(f"Another {component.type.lower()} already uses this name")

    if details.frequency == Frequency.ONE_TIME and not details.one_time_date:
        errs["frequency"].append("Pick the one-time date")
    if details.frequency == Frequency.RECURRING and details.recurring_interval not in kind["intervals"]:
        errs["frequency"].append("Pick an interval")

    if details.eligibility_mode == EligibilityMode.SPECIFIC and not details.employee_lists.filter(
        list_type=ListType.INCLUDE
    ).exists():
        errs["eligibility"].append("Add at least one worker to the include list")
    if details.eligibility_mode == EligibilityMode.CONDITION:
        conditions = list(details.conditions.all())
        if not conditions:
            errs["eligibility"].append("Add at least one condition, or switch to Include All")
        for cond in conditions:
            name = cond.get_field_display()
            if cond.field in LIST_CONDITION_FIELDS and not cond.values:
                errs["eligibility"].append(f"{name}: pick at least one value")
            if cond.field in NUMBER_CONDITION_FIELDS:
                if cond.value_from is None:
                    errs["eligibility"].append(f"{name}: enter a value")
                if cond.operator == "BETWEEN" and cond.value_to is None:
                    errs["eligibility"].append(f"{name}: enter the upper value")
                if cond.field == ConditionField.WAGE_BASE and not cond.values:
                    errs["eligibility"].append("Wage Base: pick the earnings to add up")

    value_calc = calc_of(details, CalculationPurpose.COMPONENT_VALUE)
    errs["calculation"] += _validate_calc(
        value_calc, component, "Calculation", CalculationPurpose.COMPONENT_VALUE
    )
    if (
        details.min_amount is not None
        and details.max_amount is not None
        and details.min_amount > details.max_amount
    ):
        errs["calculation"].append("Min limit is above max limit")
    if (
        component.type == ComponentType.EARNING
        and details.tax_treatment == TaxTreatment.TAX_FREE
        and details.exemption_type == ExemptionType.UP_TO_LIMIT
    ):
        errs["tax"] += _validate_calc(
            calc_of(details, CalculationPurpose.EXEMPTION_LIMIT),
            component,
            "Exemption limit",
            CalculationPurpose.EXEMPTION_LIMIT,
        )

    employer = calc_of(details, CalculationPurpose.EMPLOYER_SHARE)
    if employer:
        if employer.mode == CalculationMode.FLAT and not (
            employer.flat_amount and employer.flat_amount > 0
        ):
            errs["employer"].append("Enter the employer amount")
        if employer.mode == CalculationMode.PERCENTAGE:
            if not (employer.rate and 0 < employer.rate <= 100):
                errs["employer"].append("Employer rate must be between 0 and 100")
            if not value_calc or value_calc.mode not in (
                CalculationMode.PERCENTAGE,
                CalculationMode.SLAB,
            ):
                errs["employer"].append(
                    "A rate needs a base: use Percentage or Slab for the deduction itself"
                )

    start, end = details.effective_from, details.effective_to
    if not start:
        errs["publish"].append("Effective From is required")
    else:
        if start < today():
            # Back-dating is allowed: payslips generated for those periods from
            # now on use this version; ones already generated stay as they are.
            warns.append(
                "Effective From is in the past: payslips already generated for those periods "
                "are not recalculated"
            )
        if end and end < start:
            errs["publish"].append("Effective To is before Effective From")
        clash = (
            published_of(component)
            .exclude(pk=details.pk)
            .filter(effective_from__gte=start)
            .first()
        )
        if clash:
            errs["publish"].append(
                f"v{clash.version_no} already starts on {clash.effective_from}; pick a later date"
            )

    if component.type == ComponentType.EARNING:
        cycle = find_cycle(details)
        if cycle:
            errs["calculation"].append(f"Circular reference: {' → '.join(cycle)}")

    for dep_id in derive_depends(details):
        dep = PayComponent.objects.entire().filter(pk=dep_id).first()
        if not dep:
            continue
        if not dep.is_active:
            warns.append(f"Uses {dep.code}, which is inactive; it counts as ₹0")
        elif not latest_published(dep):
            warns.append(f"Uses {dep.code}, which isn't published yet; it counts as ₹0")
    if value_calc and value_calc.mode == CalculationMode.SYSTEM_COMPUTED:
        warns.append("System-Computed produces no figure until the Income Tax / TDS engine is built")
    if employer and (details.max_amount is not None or details.min_amount is not None):
        warns.append(
            "The max / min limit applies to the employee share only; the employer share is not limited"
        )

    errs = {k: v for k, v in errs.items() if v}
    return {"errors": errs, "warnings": warns, "total": sum(len(v) for v in errs.values())}
