"""
Create, update, publish and deactivate earnings and deductions.

These are the only write paths: the REST API and the Configuration UI both call
them. Each runs in one database transaction and either saves everything or
nothing (``PayComponentError`` rolls the transaction back). Every row written
is audited by Horilla's audit (django-auditlog).
"""

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from django.db import transaction

from employee.models import Employee
from krew_payroll.methods.pay_components import rules
from krew_payroll.methods.pay_components.serializers import component_json
from krew_payroll.models.pay_components import (
    LIST_CONDITION_FIELDS,
    NUMBER_CONDITION_FIELDS,
    Calculation,
    CalculationMode,
    CalculationPurpose,
    CalculationSlab,
    ComponentType,
    DisbursementTiming,
    EligibilityCondition,
    EligibilityEmployee,
    EligibilityMode,
    ExemptionType,
    Frequency,
    ListType,
    PayComponent,
    PayComponentDetails,
    PublishState,
    TaxTreatment,
)

READ_ONLY_KEYS = {
    "id",
    "type",
    "code",
    "company_id",
    "version_no",
    "publish_state",
    "depends_on",
    "is_active",
    "version",
}
EDITABLE_KEYS = {
    "common": {
        "name",
        "frequency",
        "recurring_interval",
        "one_time_date",
        "eligibility_mode",
        "conditions",
        "employees",
        "calculations",
        "min_amount",
        "max_amount",
        "show_on_payslip",
        "payslip_label",
        "display_order",
        "effective_from",
        "effective_to",
    },
    ComponentType.EARNING: {"disbursement_timing", "tax_treatment", "exemption_type", "requires_proof"},
    ComponentType.DEDUCTION: {"reduces_taxable_income"},
}
MONEY_KEYS = ("min_amount", "max_amount")
DATE_KEYS = ("one_time_date", "effective_from", "effective_to")
BOOL_KEYS = ("show_on_payslip", "requires_proof", "reduces_taxable_income")


class PayComponentError(Exception):
    """A request that can't be applied. ``status`` is the HTTP status to return."""

    def __init__(self, status, payload):
        super().__init__(payload)
        self.status = status
        self.payload = payload


def _label(type_):
    return "Earning" if type_ == ComponentType.EARNING else "Deduction"


def _to_decimal(value):
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _to_date(value):
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def _is_number(value):
    return value is None or (isinstance(value, (int, float, Decimal)) and not isinstance(value, bool))


def _is_date(value):
    if value is None:
        return True
    if not isinstance(value, str):
        return False
    try:
        date.fromisoformat(value)
        return len(value) == 10
    except ValueError:
        return False


# ------------------------------------------------------------------ lookups


def get_component(type_, component_id):
    """A component of this route's type in the caller's company, else 404."""
    component = PayComponent.objects.filter(pk=component_id, type=type_).first()
    if not component:
        raise PayComponentError(
            404, {"detail": f"{_label(type_)} {component_id} not found in this company"}
        )
    return component


def get_version(component, version_no):
    details = rules.versions_of(component).filter(version_no=version_no).first()
    if not details:
        raise PayComponentError(
            404, {"detail": f"Version {version_no} of {component.code} not found in this company"}
        )
    return details


# ------------------------------------------------------------------ 1. create


@transaction.atomic
def create_component(type_, body):
    """POST: create a component and its DRAFT v1. Body: name (required), display_order."""
    from base.auth_backends import resolve_company_id_for_new_record

    if not isinstance(body, dict):
        raise PayComponentError(400, {"errors": {"detail": ["Body must be a JSON object"]}})
    errors = {}
    for key in body:
        if key not in ("name", "display_order"):
            errors.setdefault(key, []).append(
                "read-only: set by the server"
                if key in READ_ONLY_KEYS
                else "not accepted on create; send it with PATCH after creating"
            )
    name = body.get("name").strip() if isinstance(body.get("name"), str) else ""
    company_id = resolve_company_id_for_new_record()
    if not company_id:
        raise PayComponentError(400, {"detail": "Select a company before creating components."})
    if not name:
        errors.setdefault("name", []).append("required")
    elif rules.company_components(company_id, type_).filter(name__iexact=name).exists():
        errors.setdefault("name", []).append(f"another {type_.lower()} already uses this name")
    order = body.get("display_order")
    if order is not None and (not isinstance(order, int) or isinstance(order, bool) or order < 0):
        errors.setdefault("display_order", []).append("must be a whole number")
    if errors:
        raise PayComponentError(400, {"errors": errors})

    kind = rules.KIND[type_]
    if order is None:
        last = (
            PayComponentDetails.objects.entire()
            .filter(component__company_id=company_id, component__type=type_)
            .order_by("-display_order")
            .values_list("display_order", flat=True)
            .first()
        )
        order = (last or 0) + 1
    component = PayComponent.objects.create(
        company_id_id=company_id, type=type_, code=rules.generate_code(company_id, name), name=name
    )
    is_earning = type_ == ComponentType.EARNING
    details = PayComponentDetails.objects.create(
        component=component,
        version_no=1,
        publish_state=PublishState.DRAFT,
        frequency=Frequency.RECURRING,
        recurring_interval=kind["intervals"][0],
        eligibility_mode=EligibilityMode.ALL,
        disbursement_timing=DisbursementTiming.FOLLOW_CADENCE if is_earning else None,
        tax_treatment=TaxTreatment.TAXABLE if is_earning else None,
        display_order=order,
        payslip_label=name,  # editable in the form; starts as the name
    )
    Calculation.objects.create(
        component_details=details,
        purpose=CalculationPurpose.COMPONENT_VALUE,
        mode=CalculationMode.FLAT,
    )
    return component, details


# ------------------------------------------------------------------ 2. update


def check_body(type_, body, details):
    """Shape checks for a PATCH body. These fail the request even for a DRAFT."""
    if not isinstance(body, dict):
        return {"detail": ["Body must be a JSON object"]}
    errors = {}

    def add(key, message):
        errors.setdefault(key, []).append(message)

    kind = rules.KIND[type_]
    allowed = EDITABLE_KEYS["common"] | EDITABLE_KEYS[type_]
    other = ComponentType.DEDUCTION if type_ == ComponentType.EARNING else ComponentType.EARNING
    company_id = details.component.company_id_id
    for key in body:
        if key in READ_ONLY_KEYS:
            add(key, "read-only: set by the server")
        elif key not in allowed:
            add(
                key,
                f"not a field of a{'n earning' if type_ == ComponentType.EARNING else ' deduction'}"
                if key in EDITABLE_KEYS[other]
                else "unknown field",
            )

    if "name" in body:
        name = body["name"]
        if not isinstance(name, str) or not name.strip():
            add("name", "can't be empty")
        elif (
            rules.company_components(company_id, type_)
            .exclude(pk=details.component_id)
            .filter(name__iexact=name.strip())
            .exists()
        ):
            add("name", f"another {type_.lower()} already uses this name")

    def in_choices(key, values):
        if key in body and body[key] is not None and body[key] not in values:
            add(key, f"must be one of {', '.join(values)}")

    if "frequency" in body and body["frequency"] not in kind["frequencies"]:
        add("frequency", f"must be one of {', '.join(kind['frequencies'])}")
    in_choices("recurring_interval", kind["intervals"])
    in_choices("eligibility_mode", EligibilityMode.values)
    if type_ == ComponentType.EARNING:
        in_choices("disbursement_timing", DisbursementTiming.values)
        in_choices("tax_treatment", TaxTreatment.values)
        in_choices("exemption_type", ExemptionType.values)
    for key in MONEY_KEYS:
        if key in body and not _is_number(body[key]):
            add(key, "must be a number or null")
    if "display_order" in body and (
        not isinstance(body["display_order"], int)
        or isinstance(body["display_order"], bool)
        or body["display_order"] < 0
    ):
        add("display_order", "must be a whole number")
    for key in DATE_KEYS:
        if key in body and not _is_date(body[key]):
            add(key, "must be YYYY-MM-DD or null")
    for key in BOOL_KEYS:
        if key in body and not isinstance(body[key], bool):
            add(key, "must be true or false")
    if "payslip_label" in body and body["payslip_label"] is not None and not isinstance(
        body["payslip_label"], str
    ):
        add("payslip_label", "must be text or null")

    if "conditions" in body:
        if not isinstance(body["conditions"], list):
            add("conditions", "must be a list")
        else:
            seen = set()
            for i, cond in enumerate(body["conditions"]):
                at = f"conditions[{i}]"
                field = cond.get("field") if isinstance(cond, dict) else None
                if field not in [f.value for f in LIST_CONDITION_FIELDS + NUMBER_CONDITION_FIELDS]:
                    add(at, "field must be one of SHIFT, STATE, SITE, CTC, WAGE_BASE, GRADE, "
                        "DESIGNATION, WORKER_CLASS, EMPLOYMENT_TYPE")
                    continue
                if field in seen:
                    add(at, f"{field} appears twice; one row per field")
                seen.add(field)
                if cond.get("operator") not in rules.operators_for(field):
                    add(at, f"operator for {field} must be {' | '.join(rules.operators_for(field))}")
                values = cond.get("values", [])
                if not isinstance(values, list):
                    add(at, "values must be a list")
                    continue
                if not _is_number(cond.get("value_from")) or not _is_number(cond.get("value_to")):
                    add(at, "value_from / value_to must be numbers")
                if field in LIST_CONDITION_FIELDS:
                    allowed_values = rules.condition_choices(field, company_id)
                    bad = [v for v in values if v not in allowed_values]
                    if bad:
                        add(at, f"unknown {field} values: {bad}")
                elif field == "WAGE_BASE":
                    earnings = set(
                        rules.company_components(company_id, ComponentType.EARNING).values_list(
                            "id", flat=True
                        )
                    )
                    if any(v not in earnings for v in values):
                        add(at, "WAGE_BASE values must be earning ids of this company")

    mode = body.get("eligibility_mode") or details.eligibility_mode
    if "employees" in body:
        if not isinstance(body["employees"], list):
            add("employees", "must be a list")
        else:
            want = ListType.INCLUDE if mode == EligibilityMode.SPECIFIC else ListType.EXCLUDE
            ids = [e.get("employee_id") for e in body["employees"] if isinstance(e, dict)]
            known = set(Employee.objects.filter(pk__in=[i for i in ids if isinstance(i, int)]).values_list("id", flat=True))
            for i, entry in enumerate(body["employees"]):
                at = f"employees[{i}]"
                if not isinstance(entry, dict) or entry.get("employee_id") not in known:
                    add(at, "unknown employee_id")
                elif entry.get("list_type") != want:
                    add(at, f"list_type must be {want} when eligibility_mode is {mode}")

    if "calculations" in body:
        calcs = body["calculations"]
        if not isinstance(calcs, list):
            add("calculations", "must be a list")
        else:
            seen = set()
            earnings = set(
                rules.company_components(company_id, ComponentType.EARNING).values_list("id", flat=True)
            )
            if not any(isinstance(c, dict) and c.get("purpose") == CalculationPurpose.COMPONENT_VALUE for c in calcs):
                add("calculations", "a COMPONENT_VALUE row is required")
            for i, calc in enumerate(calcs):
                at = f"calculations[{i}]"
                purpose = calc.get("purpose") if isinstance(calc, dict) else None
                if purpose not in kind["purposes"]:
                    add(at, f"purpose must be one of {', '.join(kind['purposes'])}")
                    continue
                if purpose in seen:
                    add(at, f"{purpose} appears twice")
                seen.add(purpose)
                modes = (
                    kind["modes"]
                    if purpose == CalculationPurpose.COMPONENT_VALUE
                    else rules.EMPLOYER_MODES
                    if purpose == CalculationPurpose.EMPLOYER_SHARE
                    else rules.EXEMPTION_MODES
                )
                if calc.get("mode") not in modes:
                    add(at, f"mode must be one of {', '.join(modes)}")
                if not _is_number(calc.get("flat_amount")) or not _is_number(calc.get("rate")):
                    add(at, "flat_amount / rate must be numbers")
                bases = calc.get("base_component_ids", [])
                if not isinstance(bases, list) or any(b not in earnings for b in bases):
                    add(at, "base_component_ids must list earning ids of this company")
                if "formula" in calc and calc["formula"] is not None and not isinstance(calc["formula"], str):
                    add(at, "formula must be text")
                slabs = calc.get("slabs", [])
                if not isinstance(slabs, list):
                    add(at, "slabs must be a list")
                else:
                    for j, slab in enumerate(slabs):
                        if not isinstance(slab, dict) or not _is_number(slab.get("range_from")) or not _is_number(
                            slab.get("range_to")
                        ) or not _is_number(slab.get("amount")):
                            add(f"{at}.slabs[{j}]", "range_from / range_to / amount must be numbers")
                        elif slab.get("applies_in_month") is not None and slab.get("applies_in_month") not in range(1, 13):
                            add(f"{at}.slabs[{j}]", "applies_in_month must be 1–12")
    return errors


def _save_calculation(details, purpose, data, is_earning):
    mode = data.get("mode")
    calc = details.calculations.filter(purpose=purpose).first() or Calculation(
        component_details=details, purpose=purpose
    )
    calc.mode = mode
    calc.flat_amount = _to_decimal(data.get("flat_amount")) if mode == CalculationMode.FLAT else None
    calc.rate = _to_decimal(data.get("rate")) if mode == CalculationMode.PERCENTAGE else None
    calc.base_component_ids = (
        sorted(set(data.get("base_component_ids") or []))
        if mode in (CalculationMode.PERCENTAGE, CalculationMode.SLAB)
        and purpose != CalculationPurpose.EMPLOYER_SHARE
        else []
    )
    calc.base_includes_ctc = bool(
        data.get("base_includes_ctc")
        and is_earning
        and mode == CalculationMode.PERCENTAGE
        and purpose != CalculationPurpose.EMPLOYER_SHARE
    )
    calc.formula = (data.get("formula") or "") if mode == CalculationMode.FORMULA else None
    calc.save()
    existing = list(calc.slabs.all().order_by("range_from", "applies_in_month"))
    wanted = (data.get("slabs") or []) if mode == CalculationMode.SLAB else []
    for i, row in enumerate(wanted):
        slab = existing[i] if i < len(existing) else CalculationSlab(calculation=calc)
        slab.range_from = _to_decimal(row.get("range_from")) or Decimal(0)
        slab.range_to = _to_decimal(row.get("range_to"))
        slab.amount = _to_decimal(row.get("amount")) or Decimal(0)
        slab.applies_in_month = row.get("applies_in_month")
        slab.save()
    for slab in existing[len(wanted):]:
        slab.delete()


def apply_body(details, body):
    """Write a (shape-checked) PATCH body into a version and its child rows."""
    component = details.component
    is_earning = component.type == ComponentType.EARNING
    if "name" in body and body["name"].strip() != component.name:
        component.name = body["name"].strip()
        component.save()

    for key in EDITABLE_KEYS["common"] | EDITABLE_KEYS[component.type]:
        if key in body and key not in ("name", "conditions", "employees", "calculations"):
            value = body[key]
            if key in MONEY_KEYS:
                value = _to_decimal(value)
            elif key in DATE_KEYS:
                value = _to_date(value)
            setattr(details, key, value)
    # Keep columns consistent with the choices made.
    if details.frequency != Frequency.RECURRING:
        details.recurring_interval = None
    if details.frequency != Frequency.ONE_TIME:
        details.one_time_date = None
    if is_earning:
        details.reduces_taxable_income = False
        if details.tax_treatment != TaxTreatment.TAX_FREE:
            details.exemption_type = None
            details.requires_proof = False
        elif not details.exemption_type:
            details.exemption_type = ExemptionType.FULL
    else:
        details.disbursement_timing = None
        details.tax_treatment = None
        details.exemption_type = None
        details.requires_proof = False
    details.save()

    if "conditions" in body:
        keep = set()
        for cond in body["conditions"]:
            row = details.conditions.filter(field=cond["field"]).first() or EligibilityCondition(
                component_details=details, field=cond["field"]
            )
            row.operator = cond["operator"]
            row.values = list(cond.get("values") or []) if cond["field"] != "CTC" else []
            is_number = cond["field"] in NUMBER_CONDITION_FIELDS
            row.value_from = _to_decimal(cond.get("value_from")) if is_number else None
            row.value_to = (
                _to_decimal(cond.get("value_to")) if is_number and cond["operator"] == "BETWEEN" else None
            )
            row.save()
            keep.add(row.pk)
        details.conditions.exclude(pk__in=keep).delete()
    if details.eligibility_mode != EligibilityMode.CONDITION:
        details.conditions.all().delete()

    if "employees" in body:
        wanted = {e["employee_id"]: e["list_type"] for e in body["employees"]}
        for row in details.employee_lists.all():
            if row.employee_id not in wanted:
                row.delete()
            elif row.list_type != wanted[row.employee_id]:
                row.list_type = wanted[row.employee_id]
                row.save()
        have = set(details.employee_lists.values_list("employee_id", flat=True))
        for employee_id, list_type in wanted.items():
            if employee_id not in have:
                EligibilityEmployee.objects.create(
                    component_details=details, employee_id=employee_id, list_type=list_type
                )
    # One list per version: the include list in SPECIFIC mode, the exclude list otherwise.
    stale = ListType.EXCLUDE if details.eligibility_mode == EligibilityMode.SPECIFIC else ListType.INCLUDE
    for row in details.employee_lists.filter(list_type=stale):
        row.delete()

    if "calculations" in body:
        sent = {c["purpose"]: c for c in body["calculations"]}
        for purpose, data in sent.items():
            _save_calculation(details, purpose, data, is_earning)
        for calc in details.calculations.exclude(purpose__in=sent.keys()):
            calc.delete()
    if not (is_earning and details.tax_treatment == TaxTreatment.TAX_FREE and details.exemption_type == ExemptionType.UP_TO_LIMIT):
        for calc in details.calculations.filter(purpose=CalculationPurpose.EXEMPTION_LIMIT):
            calc.delete()

    depends = rules.derive_depends(details)
    if depends != list(details.depends_on or []):
        details.depends_on = depends
        details.save()


@transaction.atomic
def update_version(details, body):
    """
    PATCH: apply only what the body carries. A DRAFT saves even when incomplete.
    A PUBLISHED version must still pass every publish check, or nothing is saved.
    Returns (details, checks).
    """
    shape = check_body(details.component.type, body, details)
    if shape:
        raise PayComponentError(
            400, {"detail": "The request has invalid fields. Nothing was saved.", "errors": shape}
        )
    apply_body(details, body)
    details.refresh_from_db()
    checks = rules.validate(details)
    if details.publish_state == PublishState.PUBLISHED and checks["total"]:
        raise PayComponentError(
            400,
            {
                "detail": f"v{details.version_no} is published, so it must still pass the publish "
                "checks. Nothing was saved.",
                "errors": checks["errors"],
            },
        )
    return details, checks


# ------------------------------------------------------------------ 3. publish


@transaction.atomic
def publish_version(details):
    """Validate, publish, and close the previous published version. Returns (details, closed, warnings)."""
    if details.publish_state == PublishState.PUBLISHED:
        raise PayComponentError(
            409, {"detail": f"v{details.version_no} is already published. Edit it with PATCH instead."}
        )
    checks = rules.validate(details)
    if checks["total"]:
        raise PayComponentError(
            400, {"detail": "Publish checks failed. Nothing was written.", "errors": checks["errors"]}
        )
    closed = []
    for prev in rules.published_of(details.component).filter(
        effective_from__lt=details.effective_from
    ):
        if prev.effective_to is None or prev.effective_to >= details.effective_from:
            prev.effective_to = details.effective_from - timedelta(days=1)
            prev.save()
            closed.append(prev)
    details.publish_state = PublishState.PUBLISHED
    details.save()
    return details, closed, checks["warnings"]


# ------------------------------------------------------------------ 4. deactivate


@transaction.atomic
def deactivate_component(component):
    """Mark inactive from the next pay cycle. Returns the active components that use it."""
    dependants = rules.dependants_of(component)
    if component.is_active:
        component.is_active = False
        component.save()
    return dependants


def deactivate_json(component, dependants):
    data = component_json(component)
    data["dependants"] = [
        {"id": d.id, "type": d.type, "code": d.code, "name": d.name} for d in dependants
    ]
    if dependants:
        data["note"] = f"From the next pay cycle these components count {component.code} as ₹0."
    return data


# ------------------------------------------------------------------ UI-only helpers


@transaction.atomic
def activate_component(component):
    if not component.is_active:
        component.is_active = True
        component.save()
    return component


@transaction.atomic
def new_version(component):
    """A DRAFT copied from the latest published version, to schedule a dated change."""
    existing = rules.draft_of(component)
    if existing:
        return existing
    source = rules.latest_published(component)
    if not source:
        raise PayComponentError(409, {"detail": "Publish the first version before adding another."})
    last_no = rules.versions_of(component).last().version_no
    copy = PayComponentDetails.objects.get(pk=source.pk)
    copy.pk = None
    copy.id = None
    copy.version_no = last_no + 1
    copy.publish_state = PublishState.DRAFT
    copy.effective_from = None
    copy.effective_to = None
    copy.save()
    for calc in source.calculations.all():
        slabs = list(calc.slabs.all())
        calc.pk = calc.id = None
        calc.component_details = copy
        calc.save()
        for slab in slabs:
            slab.pk = slab.id = None
            slab.calculation = calc
            slab.save()
    for cond in source.conditions.all():
        cond.pk = cond.id = None
        cond.component_details = copy
        cond.save()
    for row in source.employee_lists.all():
        row.pk = row.id = None
        row.component_details = copy
        row.save()
    return copy


@transaction.atomic
def delete_draft(details):
    if details.publish_state != PublishState.DRAFT:
        raise PayComponentError(409, {"detail": "Only a draft can be deleted."})
    component = details.component
    details.delete()
    if not rules.versions_of(component).exists():
        component.delete()
