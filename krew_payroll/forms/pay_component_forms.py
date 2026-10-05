"""Forms for earnings / deductions configuration and worker pay data."""

from django import forms
from django.apps import apps
from django.utils.translation import gettext_lazy as _

from base.forms import ModelForm
from base.models import Grade, WorkerClass
from krew_payroll.methods.pay_components import rules
from krew_payroll.models.pay_components import EmployeePayProfile, WorkSite


class PayComponentCreateForm(forms.Form):
    """New earning / deduction: only the name; everything else is set in the editor."""

    name = forms.CharField(
        max_length=100,
        label=_("Name"),
        widget=forms.TextInput(
            attrs={"class": "oh-input w-100", "placeholder": _("e.g. Night Shift Allowance")}
        ),
    )
    display_order = forms.IntegerField(
        required=False,
        min_value=0,
        label=_("Display order"),
        widget=forms.NumberInput(attrs={"class": "oh-input w-100", "placeholder": _("Optional")}),
    )


class WorkSiteForm(ModelForm):
    # The generic form template renders any field named "state" / "country" as
    # the free-text address picker (options filled by JS), so the GST state is
    # posted as "site_state" and copied onto WorkSite.state.
    site_state = forms.ModelChoiceField(
        queryset=apps.get_model("krew_company_onboarding", "GSTStateConfig").objects.none(),
        label=_("State"),
    )
    cols = {"site_state": 12, "name": 12}

    class Meta:
        model = WorkSite
        fields = ["name"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from base.auth_backends import resolve_company_id_for_new_record

        company_id = self.instance.company_id_id or resolve_company_id_for_new_record()
        self.fields["site_state"].queryset = rules.company_states(company_id)
        self.fields["site_state"].widget.attrs.update({"class": "oh-select oh-select-2"})
        if self.instance.pk:
            self.fields["site_state"].initial = self.instance.state_id
        self.order_fields(["site_state", "name"])

    def clean(self):
        cleaned = super().clean()
        from base.auth_backends import resolve_company_id_for_new_record

        company_id = self.instance.company_id_id or resolve_company_id_for_new_record()
        name = (cleaned.get("name") or "").strip()
        state = cleaned.get("site_state")
        if company_id and state and name:
            clash = WorkSite.objects.entire().filter(
                company_id=company_id, state=state, name__iexact=name
            )
            if self.instance.pk:
                clash = clash.exclude(pk=self.instance.pk)
            if clash.exists():
                self.add_error("name", _("This site already exists in that state."))
        cleaned["name"] = name
        return cleaned

    def save(self, commit=True):
        self.instance.state = self.cleaned_data["site_state"]
        return super().save(commit=commit)


class EmployeePayProfileForm(ModelForm):
    """A worker's pay attributes, edited from the employee profile."""

    cols = {
        "ctc_annual": 6,
        "payout_cadence": 6,
        "worker_class": 6,
        "employment_type": 6,
        "grade": 6,
        "work_state": 6,
        "work_site": 6,
    }

    class Meta:
        model = EmployeePayProfile
        fields = [
            "ctc_annual",
            "payout_cadence",
            "worker_class",
            "employment_type",
            "grade",
            "work_state",
            "work_site",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        company_id = self.instance.company_id_id
        if not company_id and getattr(self.instance, "employee_id", None):
            work_info = getattr(self.instance.employee, "employee_work_info", None)
            company_id = work_info.company_id_id if work_info else None
        self.fields["worker_class"].queryset = WorkerClass.objects.entire().filter(
            company_id=company_id
        )
        self.fields["grade"].queryset = Grade.objects.entire().filter(company_id=company_id)
        self.fields["work_state"].queryset = rules.company_states(company_id)
        self.fields["work_site"].queryset = WorkSite.objects.entire().filter(
            company_id=company_id
        )
        self.fields["ctc_annual"].label = _("CTC (annual ₹)")

    def clean(self):
        cleaned = super().clean()
        site, state = cleaned.get("work_site"), cleaned.get("work_state")
        if site and state and site.state_id != state.id:
            self.add_error("work_site", _("This site is not in the selected work state."))
        if site and not state:
            cleaned["work_state"] = site.state
        return cleaned
