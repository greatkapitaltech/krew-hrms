"""
Earnings / deductions editor (Payroll → Configuration).

The editor is a server-rendered HTMX form. Structural changes (mode switches,
adding a condition or a slab row) re-render the form from the unsaved posted
state. Nothing is stored until Save, which sends the same body the PATCH API
takes through ``services.update_version``. Publish saves pending edits first.
"""

from datetime import date
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_http_methods

from base.models import EmployeeShift, Grade, JobPosition, WorkerClass
from employee.models import Employee
from horilla.decorators import login_required, permission_required
from krew_payroll.forms.pay_component_forms import PayComponentCreateForm
from krew_payroll.methods.pay_components import engine, rules, services
from krew_payroll.methods.pay_components.describe import describe_calculation
from krew_payroll.methods.pay_components.serializers import editable_body
from krew_payroll.methods.pay_components.services import PayComponentError
from krew_payroll.models.pay_components import (
    LIST_CONDITION_FIELDS,
    CalculationMode,
    CalculationPurpose,
    ComponentType,
    ConditionField,
    DisbursementTiming,
    EligibilityMode,
    EmploymentType,
    ExemptionType,
    Frequency,
    ListType,
    PayComponent,
    PublishState,
    RecurringInterval,
    TaxTreatment,
    WorkSite,
)

ROUTES = {"earnings": ComponentType.EARNING, "deductions": ComponentType.DEDUCTION}
# Short keys used in form field names for each calculation purpose.
PURPOSE_KEYS = {
    "value": CalculationPurpose.COMPONENT_VALUE,
    "exemption": CalculationPurpose.EXEMPTION_LIMIT,
    "employer": CalculationPurpose.EMPLOYER_SHARE,
}
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


# ------------------------------------------------------------------ helpers


def _type_for(route):
    if route not in ROUTES:
        raise Http404
    return ROUTES[route]


def _component(route, pk):
    try:
        return services.get_component(_type_for(route), pk)
    except PayComponentError as exc:
        raise Http404 from exc


def _number(value):
    value = (value or "").strip() if isinstance(value, str) else value
    if value in (None, ""):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return int(number) if number == number.to_integral_value() else float(number)


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _date(value):
    return value or None


def _redirect(url):
    response = HttpResponse()
    response["HX-Redirect"] = url
    return response


def _editor_url(component, version_no=None):
    url = reverse("pay-component-editor", kwargs={"route": component.route, "pk": component.pk})
    return f"{url}?v={version_no}" if version_no else url


# ------------------------------------------------------------------ form <-> state


def state_from_post(component, post):
    """Rebuild the editor state (PATCH-body shape) from the posted form, then apply any op."""
    is_earning = component.type == ComponentType.EARNING
    freq = post.get("freq") or "RECURRING"
    frequency, _, interval = freq.partition(":")
    state = {
        "name": (post.get("name") or "").strip(),
        "frequency": frequency,
        "recurring_interval": interval or None,
        "one_time_date": _date(post.get("one_time_date")),
        "eligibility_mode": post.get("eligibility_mode") or EligibilityMode.ALL,
        "min_amount": _number(post.get("min_amount")),
        "max_amount": _number(post.get("max_amount")),
        "show_on_payslip": post.get("show_on_payslip") == "on",
        "payslip_label": (post.get("payslip_label") or "").strip() or None,
        "display_order": _int(post.get("display_order")) or 0,
        "effective_from": _date(post.get("effective_from")),
        "effective_to": _date(post.get("effective_to")),
    }
    if is_earning:
        state["disbursement_timing"] = post.get("disbursement_timing") or DisbursementTiming.FOLLOW_CADENCE
        state["tax_treatment"] = post.get("tax_treatment") or TaxTreatment.TAXABLE
        state["exemption_type"] = (
            post.get("exemption_type") or ExemptionType.FULL
            if state["tax_treatment"] == TaxTreatment.TAX_FREE
            else None
        )
        state["requires_proof"] = post.get("requires_proof") == "on"
    else:
        state["reduces_taxable_income"] = post.get("reduces_taxable_income") == "on"

    conditions = []
    for field in post.getlist("cond_fields"):
        is_list = field in [f.value for f in LIST_CONDITION_FIELDS]
        raw_values = post.getlist(f"cond_{field}_values")
        if field == ConditionField.EMPLOYMENT_TYPE:
            values = [v for v in raw_values if v]
        else:
            values = [i for i in (_int(v) for v in raw_values) if i is not None]
        conditions.append(
            {
                "field": field,
                "operator": "IN" if is_list else post.get(f"cond_{field}_operator") or "EQ",
                "values": values if field != "CTC" else [],
                "value_from": None if is_list else _number(post.get(f"cond_{field}_value_from")),
                "value_to": None if is_list else _number(post.get(f"cond_{field}_value_to")),
            }
        )
    state["conditions"] = conditions

    if state["eligibility_mode"] == EligibilityMode.SPECIFIC:
        ids, list_type = post.getlist("include_ids"), ListType.INCLUDE
    else:
        ids, list_type = post.getlist("exclude_ids"), ListType.EXCLUDE
    state["employees"] = [
        {"employee_id": i, "list_type": list_type} for i in (_int(v) for v in ids) if i is not None
    ]

    calculations = []
    for key, purpose in PURPOSE_KEYS.items():
        mode = post.get(f"calc_{key}_mode")
        if not mode or mode == "NONE":
            continue
        slabs = []
        for i in range(_int(post.get(f"calc_{key}_slab_count")) or 0):
            slabs.append(
                {
                    "range_from": _number(post.get(f"calc_{key}_slab_{i}_from")),
                    "range_to": _number(post.get(f"calc_{key}_slab_{i}_to")),
                    "amount": _number(post.get(f"calc_{key}_slab_{i}_amount")),
                    "applies_in_month": _int(post.get(f"calc_{key}_slab_{i}_month")),
                }
            )
        calculations.append(
            {
                "purpose": purpose,
                "mode": mode,
                "flat_amount": _number(post.get(f"calc_{key}_flat_amount")),
                "rate": _number(post.get(f"calc_{key}_rate")),
                "base_component_ids": [
                    i for i in (_int(v) for v in post.getlist(f"calc_{key}_bases")) if i is not None
                ],
                "base_includes_ctc": post.get(f"calc_{key}_ctc") == "on",
                "formula": post.get(f"calc_{key}_formula") or "",
                "slabs": slabs,
            }
        )
    state["calculations"] = calculations
    _apply_op(state, post)
    return state


def _calc(state, purpose):
    return next((c for c in state["calculations"] if c["purpose"] == purpose), None)


def _apply_op(state, post):
    """Structural edits that only change the unsaved form."""
    field = post.get("op_add_condition")
    if field and field not in [c["field"] for c in state["conditions"]]:
        is_list = field in [f.value for f in LIST_CONDITION_FIELDS]
        state["conditions"].append(
            {"field": field, "operator": "IN" if is_list else "LT", "values": [], "value_from": None, "value_to": None}
        )
    remove = post.get("op_remove_condition")
    if remove:
        state["conditions"] = [c for c in state["conditions"] if c["field"] != remove]
    key = post.get("op_add_slab")
    if key in PURPOSE_KEYS and _calc(state, PURPOSE_KEYS[key]):
        slabs = _calc(state, PURPOSE_KEYS[key])["slabs"]
        last = slabs[-1] if slabs else None
        start = (last["range_to"] + 0.01) if last and last.get("range_to") is not None else None
        slabs.append({"range_from": start, "range_to": None, "amount": None, "applies_in_month": None})
    remove_slab = post.get("op_remove_slab")
    if remove_slab and ":" in remove_slab:
        key, index = remove_slab.split(":", 1)
        calc = _calc(state, PURPOSE_KEYS.get(key))
        if calc and (_int(index) is not None) and _int(index) < len(calc["slabs"]):
            calc["slabs"].pop(_int(index))
    token = post.get("op_formula_token")
    if token and ":" in token:
        key, value = token.split(":", 1)
        calc = _calc(state, PURPOSE_KEYS.get(key))
        if calc:
            if value == "BACK":
                calc["formula"] = " ".join(calc["formula"].split()[:-1])
            elif value == "CLEAR":
                calc["formula"] = ""
            else:
                calc["formula"] = " ".join([*calc["formula"].split(), value])


def body_from_state(component, state):
    """The PATCH body for a state: only the keys this component type accepts."""
    body = {k: v for k, v in state.items()}
    calcs = []
    for calc in state["calculations"]:
        data = {k: v for k, v in calc.items() if k != "slabs" or calc["mode"] == CalculationMode.SLAB}
        calcs.append(data)
    body["calculations"] = calcs
    return body


# ------------------------------------------------------------------ editor context


def _live_summary(component, state, freq_options, freq_value):
    """Summary lines for the form as it is now (saved or not)."""
    from types import SimpleNamespace

    company_id = component.company_id_id

    def as_calc(data):
        if not data:
            return None
        return SimpleNamespace(
            mode=data.get("mode"),
            flat_amount=data.get("flat_amount"),
            rate=data.get("rate"),
            base_component_ids=data.get("base_component_ids") or [],
            base_includes_ctc=bool(data.get("base_includes_ctc")),
            formula=data.get("formula") or "",
            slabs=data.get("slabs") or [],
        )

    value = as_calc(_calc(state, CalculationPurpose.COMPONENT_VALUE))
    ids = set()
    for purpose in (CalculationPurpose.COMPONENT_VALUE, CalculationPurpose.EXEMPTION_LIMIT):
        ids |= rules.calc_references(as_calc(_calc(state, purpose)), company_id)
    for cond in state["conditions"]:
        if cond["field"] == ConditionField.WAGE_BASE:
            ids |= {int(v) for v in cond.get("values") or []}
    ids.discard(component.pk)
    return {
        "summary_value": describe_calculation(value),
        "summary_when": dict(freq_options).get(freq_value, "-"),
        "summary_who": EligibilityMode(state["eligibility_mode"]).label,
        "summary_depends": list(
            PayComponent.objects.entire().filter(pk__in=ids).order_by("code").values_list("code", flat=True)
        ),
    }


def _value_options(field, company_id, state_filter=None):
    if field == ConditionField.SHIFT:
        return [(s.id, s.employee_shift) for s in EmployeeShift.objects.all()]
    if field == ConditionField.STATE:
        return [(s.id, s.name) for s in rules.company_states(company_id)]
    if field == ConditionField.SITE:
        sites = WorkSite.objects.entire().filter(company_id=company_id)
        if state_filter:
            sites = sites.filter(state_id__in=state_filter)
        return [(s.id, str(s)) for s in sites]
    if field == ConditionField.GRADE:
        return [(g.id, f"{g.code} · {g.name}") for g in Grade.objects.entire().filter(company_id=company_id)]
    if field == ConditionField.DESIGNATION:
        return [(j.id, j.job_position) for j in JobPosition.objects.all()]
    if field == ConditionField.WORKER_CLASS:
        return [(w.id, w.name) for w in WorkerClass.objects.entire().filter(company_id=company_id)]
    if field == ConditionField.EMPLOYMENT_TYPE:
        return list(EmploymentType.choices)
    return []


def _rule_is_complete(cond):
    """A rule with nothing picked yet is ignored when listing eligible employees."""
    if cond["field"] in [f.value for f in LIST_CONDITION_FIELDS]:
        return bool(cond.get("values"))
    if cond.get("value_from") is None:
        return False
    return cond.get("operator") != "BETWEEN" or cond.get("value_to") is not None


def _employee_choices(component, state):
    """
    (include options, exclude options, wage_base_pending) for the employee pickers.

    Only the component's company's active employees. In Condition-Based mode the
    exclude list holds just the employees the (unsaved) rules let in, matched
    with the pay run's own rule check; Wage Base needs a pay run's earnings, so
    it is left out here (wage_base_pending tells the template to say so).
    """
    from types import SimpleNamespace

    employees = list(
        Employee.objects.entire()
        .filter(is_active=True, employee_work_info__company_id=component.company_id_id)
        .select_related("employee_work_info")
        .order_by("employee_first_name", "employee_last_name")
    )
    label = lambda e: f"{e.get_full_name()} ({e.badge_id})" if e.badge_id else e.get_full_name()
    everyone = [(e.id, label(e)) for e in employees]
    if state["eligibility_mode"] != EligibilityMode.CONDITION:
        return everyone, everyone, False
    rules_now = [c for c in state["conditions"] if _rule_is_complete(c)]
    checkable = [SimpleNamespace(**c) for c in rules_now if c["field"] != ConditionField.WAGE_BASE]
    workers = engine.workers_for(employees)
    eligible = [
        (e.id, label(e))
        for e in employees
        if all(engine.condition_matches(cond, workers[e.pk]) for cond in checkable)
    ]
    pending = any(c["field"] == ConditionField.WAGE_BASE for c in rules_now)
    return everyone, eligible, pending


def editor_context(request, component, details, state, *, dirty, errors=None, message=None):
    kind = rules.KIND[component.type]
    is_earning = component.type == ComponentType.EARNING
    company_id = component.company_id_id
    earnings = rules.company_components(company_id, ComponentType.EARNING).exclude(pk=component.pk)
    earnings_options = [(e.id, e.name, e.code) for e in earnings]
    state_condition = next((c for c in state["conditions"] if c["field"] == ConditionField.STATE), None)
    conditions = []
    for cond in state["conditions"]:
        is_list = cond["field"] in [f.value for f in LIST_CONDITION_FIELDS]
        conditions.append(
            {
                **cond,
                "label": ConditionField(cond["field"]).label,
                "is_list": is_list,
                "options": _value_options(
                    cond["field"], company_id, state_condition["values"] if state_condition else None
                )
                if is_list
                else [],
                "operators": [(o, o) for o in rules.operators_for(cond["field"])],
            }
        )
    used = {c["field"] for c in state["conditions"]}
    include_options, exclude_options, wage_base_pending = _employee_choices(component, state)
    freq_options = [(f"RECURRING:{i}", RecurringInterval(i).label) for i in kind["intervals"]] + [
        (f, Frequency(f).label) for f in kind["frequencies"] if f != Frequency.RECURRING
    ]
    freq_value = (
        f"RECURRING:{state['recurring_interval']}"
        if state["frequency"] == Frequency.RECURRING
        else state["frequency"]
    )
    for calc in state["calculations"]:
        calc.setdefault("slabs", [])
        calc["formula"] = calc.get("formula") or ""
    calcs = {}
    for key, purpose in PURPOSE_KEYS.items():
        calc = _calc(state, purpose)
        calcs[key] = calc or {
            "purpose": purpose,
            "mode": "NONE" if key == "employer" else (CalculationMode.PERCENTAGE if key == "exemption" else CalculationMode.FLAT),
            "flat_amount": None,
            "rate": None,
            "base_component_ids": [],
            "base_includes_ctc": False,
            "formula": "",
            "slabs": [],
        }
    versions = list(rules.versions_of(component))
    # Any Effective From (past dates too); Effective To can't be before it.
    is_published = details.publish_state == PublishState.PUBLISHED
    chosen_from = state.get("effective_from")
    chosen_from = date.fromisoformat(chosen_from) if isinstance(chosen_from, str) and chosen_from else chosen_from
    to_floor = chosen_from
    perms = request.user
    return {
        "component": component,
        "details": details,
        "route": component.route,
        "is_earning": is_earning,
        "is_published": is_published,
        "to_min": to_floor.isoformat() if to_floor else "",
        "can_edit": perms.has_perm("krew_payroll.change_paycomponent"),
        "state": state,
        "dirty": dirty,
        "errors": errors or {},
        "error_total": sum(len(v) for v in (errors or {}).values()),
        "message": message,
        "versions": versions,
        "draft": rules.draft_of(component),
        "has_published": rules.latest_published(component) is not None,
        "freq_options": freq_options,
        "freq_value": freq_value,
        "disbursement_options": DisbursementTiming.choices,
        "eligibility_options": EligibilityMode.choices,
        "condition_rows": conditions,
        "condition_add_options": [
            (f.value, f.label) for f in ConditionField if f.value not in used
        ],
        "include_options": include_options,
        "exclude_options": exclude_options,
        "wage_base_pending": wage_base_pending,
        "include_ids": [e["employee_id"] for e in state["employees"] if e["list_type"] == ListType.INCLUDE],
        "exclude_ids": [e["employee_id"] for e in state["employees"] if e["list_type"] == ListType.EXCLUDE],
        "earning_options": earnings_options,
        "value_modes": [(m, CalculationMode(m).label) for m in kind["modes"]],
        "exemption_modes": [(m, CalculationMode(m).label) for m in rules.EXEMPTION_MODES],
        "employer_modes": [
            ("NONE", _("None")),
            (CalculationMode.FLAT, _("Flat amount")),
            (CalculationMode.PERCENTAGE, _("Rate % of the same base")),
        ],
        "calc": calcs,
        "formula_codes": (["CTC"] if kind["ctc_in_formula"] else []) + [e.code for e in earnings],
        "tax_options": TaxTreatment.choices,
        "exemption_options": ExemptionType.choices,
        "month_options": [(i + 1, MONTHS[i]) for i in range(12)],
        **_live_summary(component, state, freq_options, freq_value),
        # Back to the list this component is in (Earnings or Deductions tab).
        "settings_url": f"{reverse('payroll-settings-view')}?tab={component.route}",
        "render_url": reverse("pay-component-render", kwargs={"route": component.route, "pk": component.pk, "version_no": details.version_no}),
        "save_url": reverse("pay-component-save", kwargs={"route": component.route, "pk": component.pk, "version_no": details.version_no}),
        "publish_url": reverse("pay-component-publish-confirm", kwargs={"route": component.route, "pk": component.pk, "version_no": details.version_no}),
        "delete_draft_url": reverse("pay-component-delete-draft", kwargs={"route": component.route, "pk": component.pk, "version_no": details.version_no}),
        "new_version_url": reverse("pay-component-new-version", kwargs={"route": component.route, "pk": component.pk}),
        "toggle_url": component.get_active_toggle_url(),
    }


def _render_editor(request, component, details, state, **kwargs):
    context = editor_context(request, component, details, state, **kwargs)
    context["oob_header"] = True  # refresh the version tabs / name above the form too
    return render(request, "payroll/pay_components/editor_form.html", context)


def _pick_version(component, version_no=None):
    if version_no:
        details = rules.versions_of(component).filter(version_no=version_no).first()
        if details:
            return details
    return rules.draft_of(component) or rules.latest_published(component) or rules.versions_of(component).last()


# ------------------------------------------------------------------ views


@login_required
@permission_required("krew_payroll.view_paycomponent")
def pay_component_editor(request, route, pk):
    """Full page: the editor for one component (``?v=`` picks the version)."""
    try:
        component = _component(route, pk)
    except Http404:
        # E.g. the company was just switched while this page was open: the
        # component belongs to another company, so go back to the list.
        if PayComponent.objects.entire().filter(pk=pk).exists():
            messages.info(request, _("That earning / deduction belongs to another company."))
            return redirect(f"{reverse('payroll-settings-view')}?tab={route}")
        raise
    details = _pick_version(component, _int(request.GET.get("v")))
    if details is None:
        raise Http404
    context = editor_context(request, component, details, editable_body(details), dirty=False)
    settings_url = context["settings_url"]
    request.page_breadcrumbs = [
        (_("Payroll"), reverse("view-payroll-dashboard")),
        (_("Configuration"), reverse("payroll-settings-view")),
        (_("Earnings") if component.type == ComponentType.EARNING else _("Deductions"), settings_url),
        (component.name, component.get_editor_url()),
    ]
    return render(request, "payroll/pay_components/editor.html", context)


@login_required
@permission_required("krew_payroll.view_paycomponent")
@require_http_methods(["POST"])
def pay_component_render(request, route, pk, version_no):
    """Re-render the editor from the unsaved posted form (structural edits)."""
    component = _component(route, pk)
    details = _pick_version(component, version_no)
    state = state_from_post(component, request.POST)
    return _render_editor(request, component, details, state, dirty=True)


@login_required
@permission_required("krew_payroll.change_paycomponent")
@require_http_methods(["POST"])
def pay_component_save(request, route, pk, version_no):
    """Save: the same body the PATCH API takes, through the same service."""
    component = _component(route, pk)
    details = _pick_version(component, version_no)
    state = state_from_post(component, request.POST)
    try:
        details, _checks = services.update_version(details, body_from_state(component, state))
    except PayComponentError as exc:
        errors = exc.payload.get("errors") or {"detail": [exc.payload.get("detail", "")]}
        return _render_editor(request, component, details, state, dirty=True, errors=errors)
    component.refresh_from_db()
    return _render_editor(
        request, component, details, editable_body(details), dirty=False, message=_("All changes saved")
    )


@login_required
@permission_required("krew_payroll.add_paycomponent")
def pay_component_create(request, route):
    """Modal: a new earning / deduction needs only a name; the editor opens next."""
    type_ = _type_for(route)
    form = PayComponentCreateForm(request.POST or None)
    if type_ == ComponentType.DEDUCTION:
        form.fields["name"].widget.attrs["placeholder"] = _("e.g. Employee PF")
    if request.method == "POST" and form.is_valid():
        body = {"name": form.cleaned_data["name"]}
        if form.cleaned_data.get("display_order") is not None:
            body["display_order"] = form.cleaned_data["display_order"]
        try:
            component, _details = services.create_component(type_, body)
        except PayComponentError as exc:
            for field, problems in (exc.payload.get("errors") or {"name": [exc.payload.get("detail")]}).items():
                for problem in problems:
                    form.add_error(field if field in form.fields else None, problem)
        else:
            messages.success(request, _("%(name)s created as a draft.") % {"name": component.name})
            return _redirect(_editor_url(component))
    return render(
        request,
        "payroll/pay_components/create_modal.html",
        {"form": form, "route": route, "is_earning": type_ == ComponentType.EARNING},
    )


@login_required
@permission_required("krew_payroll.change_paycomponent")
@require_http_methods(["POST"])
def pay_component_publish_confirm(request, route, pk, version_no):
    """Modal: save pending edits, then show the publish checks or the confirmation."""
    component = _component(route, pk)
    details = _pick_version(component, version_no)
    errors = None
    if request.POST.get("dirty") == "1":
        state = state_from_post(component, request.POST)
        try:
            details, _checks = services.update_version(details, body_from_state(component, state))
        except PayComponentError as exc:
            errors = exc.payload.get("errors") or {"detail": [exc.payload.get("detail", "")]}
    checks = rules.validate(details)
    closing = [
        v
        for v in rules.published_of(component)
        if details.effective_from
        and v.effective_from < details.effective_from
        and (v.effective_to is None or v.effective_to >= details.effective_from)
    ]
    context = {
        "component": component,
        "details": details,
        "errors": errors or checks["errors"],
        "warnings": checks["warnings"],
        "closing": closing,
        "saved_now": request.POST.get("dirty") == "1" and not errors,
        "publish_url": reverse("pay-component-publish", kwargs={"route": route, "pk": pk, "version_no": details.version_no}),
        "editor_url": _editor_url(component, details.version_no),
    }
    return render(request, "payroll/pay_components/publish_modal.html", context)


@login_required
@permission_required("krew_payroll.change_paycomponent")
@require_http_methods(["POST"])
def pay_component_publish(request, route, pk, version_no):
    component = _component(route, pk)
    details = _pick_version(component, version_no)
    try:
        details, closed, _warnings = services.publish_version(details)
    except PayComponentError as exc:
        messages.error(request, exc.payload.get("detail", _("Publish failed.")))
        return _redirect(_editor_url(component, version_no))
    note = (
        _(" v%(v)s now ends on %(d)s.") % {"v": closed[0].version_no, "d": closed[0].effective_to}
        if closed
        else ""
    )
    messages.success(
        request,
        _("%(name)s v%(v)s published, live from %(d)s.")
        % {"name": component.name, "v": details.version_no, "d": details.effective_from}
        + note,
    )
    return _redirect(_editor_url(component, details.version_no))


@login_required
@permission_required("krew_payroll.change_paycomponent")
@require_http_methods(["POST"])
def pay_component_delete_draft(request, route, pk, version_no):
    component = _component(route, pk)
    details = _pick_version(component, version_no)
    try:
        services.delete_draft(details)
    except PayComponentError as exc:
        messages.error(request, exc.payload["detail"])
        return _redirect(_editor_url(component, version_no))
    messages.success(request, _("Draft deleted."))
    if PayComponent.objects.filter(pk=pk).exists():
        return _redirect(_editor_url(component))
    return _redirect(f"{reverse('payroll-settings-view')}?tab={component.route}")


@login_required
@permission_required("krew_payroll.change_paycomponent")
@require_http_methods(["POST"])
def pay_component_new_version(request, route, pk):
    component = _component(route, pk)
    try:
        details = services.new_version(component)
    except PayComponentError as exc:
        messages.error(request, exc.payload["detail"])
        return _redirect(_editor_url(component))
    return _redirect(_editor_url(component, details.version_no))


@login_required
@permission_required("krew_payroll.change_paycomponent")
def pay_component_active_toggle(request, route, pk):
    """Modal: deactivate (listing the components that use it) or activate."""
    component = _component(route, pk)
    if request.method == "POST":
        if component.is_active:
            services.deactivate_component(component)
            messages.success(request, _("%(name)s deactivated.") % {"name": component.name})
        else:
            services.activate_component(component)
            messages.success(request, _("%(name)s activated.") % {"name": component.name})
        response = HttpResponse()
        response["HX-Refresh"] = "true"
        return response
    return render(
        request,
        "payroll/pay_components/active_toggle_modal.html",
        {"component": component, "dependants": rules.dependants_of(component) if component.is_active else []},
    )


# ------------------------------------------------------------------ employee pay profile tab


def pay_profile_accessibility(request, instance=None, user_perms=None, *args, **kwargs):
    """Payroll staff, or the employee viewing their own profile."""
    employee = getattr(request.user, "employee_get", None)
    return request.user.has_perm("krew_payroll.view_employeepayprofile") or (
        instance is not None and employee is not None and instance == employee
    )


@login_required
def pay_profile_tab(request, pk, *args, **kwargs):
    """Employee profile tab: the worker's pay attributes (CTC, class, grade, state, site, cadence)."""
    from krew_payroll.forms.pay_component_forms import EmployeePayProfileForm
    from krew_payroll.models.pay_components import EmployeePayProfile

    employee = Employee.objects.entire().filter(pk=pk).first()
    if employee is None:
        raise Http404
    own = getattr(request.user, "employee_get", None) == employee
    if not (request.user.has_perm("krew_payroll.view_employeepayprofile") or own):
        raise Http404
    can_edit = request.user.has_perm("krew_payroll.change_employeepayprofile") and request.user.has_perm(
        "krew_payroll.add_employeepayprofile"
    )
    profile = EmployeePayProfile.objects.entire().filter(employee=employee).first() or EmployeePayProfile(
        employee=employee
    )
    saved = False
    if request.method == "POST":
        if not can_edit:
            raise Http404
        form = EmployeePayProfileForm(request.POST, instance=profile)
        if form.is_valid():
            form.save()
            saved = True
            form = EmployeePayProfileForm(instance=profile)
    else:
        form = EmployeePayProfileForm(instance=profile)
    if not can_edit:
        for field in form.fields.values():
            field.disabled = True
    return render(
        request,
        "payroll/pay_components/pay_profile_tab.html",
        {
            "form": form,
            "employee": employee,
            "profile": profile,
            "can_edit": can_edit,
            "saved": saved,
            "save_url": reverse("employee-pay-profile-tab", kwargs={"pk": employee.pk}),
        },
    )
