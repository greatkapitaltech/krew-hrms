"""JSON shapes returned by the earnings / deductions APIs."""

from decimal import Decimal

from krew_payroll.methods.pay_components.rules import KIND
from krew_payroll.models.pay_components import ComponentType


def num(value):
    """Decimal -> int when whole, else float; None stays None."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


def iso(day):
    return day.isoformat() if day else None


def calculation_json(calc):
    data = {
        "purpose": calc.purpose,
        "mode": calc.mode,
        "flat_amount": num(calc.flat_amount),
        "rate": num(calc.rate),
        "base_component_ids": list(calc.base_component_ids or []),
        "base_includes_ctc": bool(calc.base_includes_ctc),
        "formula": calc.formula,
    }
    if calc.mode == "SLAB":
        data["slabs"] = [
            {
                "range_from": num(s.range_from),
                "range_to": num(s.range_to),
                "amount": num(s.amount),
                "applies_in_month": s.applies_in_month,
            }
            for s in calc.slabs.all().order_by("range_from", "applies_in_month")
        ]
    return data


def version_json(details):
    component = details.component
    is_earning = component.type == ComponentType.EARNING
    order = KIND[component.type]["purposes"]
    calcs = sorted(details.calculations.all(), key=lambda c: order.index(c.purpose))
    data = {
        "version_no": details.version_no,
        "publish_state": details.publish_state,
        "effective_from": iso(details.effective_from),
        "effective_to": iso(details.effective_to),
        "frequency": details.frequency,
        "recurring_interval": details.recurring_interval,
        "one_time_date": iso(details.one_time_date),
    }
    if is_earning:
        data["disbursement_timing"] = details.disbursement_timing
    data.update(
        {
            "eligibility_mode": details.eligibility_mode,
            "conditions": [
                {
                    "field": c.field,
                    "operator": c.operator,
                    "values": list(c.values or []),
                    "value_from": num(c.value_from),
                    "value_to": num(c.value_to),
                }
                for c in details.conditions.all().order_by("id")
            ],
            "employees": [
                {"employee_id": e.employee_id, "list_type": e.list_type}
                for e in details.employee_lists.all().order_by("id")
            ],
            "calculations": [calculation_json(c) for c in calcs],
            "min_amount": num(details.min_amount),
            "max_amount": num(details.max_amount),
        }
    )
    if is_earning:
        data.update(
            {
                "tax_treatment": details.tax_treatment,
                "exemption_type": details.exemption_type,
                "requires_proof": bool(details.requires_proof),
            }
        )
    else:
        data["reduces_taxable_income"] = bool(details.reduces_taxable_income)
    data.update(
        {
            "show_on_payslip": bool(details.show_on_payslip),
            "payslip_label": details.payslip_label,
            "display_order": details.display_order,
            "depends_on": list(details.depends_on or []),
        }
    )
    return data


def component_json(component, details=None):
    data = {
        "id": component.id,
        "type": component.type,
        "code": component.code,
        "name": component.name,
        "is_active": bool(component.is_active),
    }
    if details is not None:
        data["version"] = version_json(details)
    return data


def editable_body(details):
    """Every editable field of a version, in PATCH-body shape (used by the UI's Save)."""
    body = {"name": details.component.name, **version_json(details)}
    for key in ("version_no", "publish_state", "depends_on"):
        body.pop(key, None)
    return body
