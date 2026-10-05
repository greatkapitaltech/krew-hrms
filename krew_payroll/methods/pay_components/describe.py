"""Human-readable one-liners for calculations (list columns, summaries)."""

from decimal import Decimal

from krew_payroll.models.pay_components import CalculationMode, PayComponent


def pretty_formula(text):
    return " ".join(str(text or "").split()).replace("*", "×").replace("/", "÷")


def base_text(calc):
    codes = dict(
        PayComponent.objects.entire()
        .filter(pk__in=calc.base_component_ids or [])
        .values_list("id", "code")
    )
    parts = (["CTC"] if calc.base_includes_ctc else []) + [
        codes[i] for i in calc.base_component_ids or [] if i in codes
    ]
    if not parts:
        return "(no base picked)"
    return f"({' + '.join(parts)})" if len(parts) > 1 else parts[0]


def describe_calculation(calc):
    if not calc:
        return "-"
    if calc.mode == CalculationMode.FLAT:
        return f"Flat ₹{Decimal(str(calc.flat_amount)):,.0f}" if calc.flat_amount is not None else "Flat amount (not set)"
    if calc.mode == CalculationMode.PERCENTAGE:
        rate = f"{Decimal(str(calc.rate)).normalize():f}" if calc.rate is not None else "?"
        return f"{rate}% of {base_text(calc)}"
    if calc.mode == CalculationMode.FORMULA:
        return pretty_formula(calc.formula) if calc.formula else "Formula (empty)"
    if calc.mode == CalculationMode.SLAB:
        slabs = calc.slabs if isinstance(calc.slabs, list) else calc.slabs.all()
        return f"Slab on {base_text(calc)} · {len(slabs)} rows"
    return "System-computed"
