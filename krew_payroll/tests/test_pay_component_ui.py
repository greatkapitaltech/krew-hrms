"""Payroll → Configuration: Earnings / Deductions screens and the masters."""

from datetime import timedelta

from django.test import TestCase
from django.urls import reverse

from base.models import Grade, WorkerClass
from horilla.horilla_middlewares import _thread_locals, set_selected_company
from horilla.testkit import make_company, make_employee, make_user
from krew_payroll.methods.pay_components import rules
from krew_payroll.models.pay_components import PayComponent, PublishState

HX = {"HTTP_HX_REQUEST": "true"}


class PayComponentUITests(TestCase):
    def setUp(self):
        self.company = make_company("Sarvam UI")
        self.user = make_user("pc_admin", password="secret123", is_superuser=True)
        self.emp = make_employee(company=self.company, email="pc_admin@test.horilla", user=self.user)
        self.client.login(username="pc_admin", password="secret123")
        session = self.client.session
        session["selected_company"] = str(self.company.id)
        session.save()

    def tearDown(self):
        set_selected_company(None)
        _thread_locals.request = None
        super().tearDown()

    def create(self, route="earnings", name="Attendance Bonus"):
        response = self.client.post(reverse("pay-component-create", kwargs={"route": route}), {"name": name}, **HX)
        self.assertEqual(response.status_code, 200)
        self.assertIn("HX-Redirect", response.headers, response.content[:400])
        return PayComponent.objects.entire().get(name=name)

    def test_configuration_tabs_lists_and_navs_render(self):
        for name in (
            "payroll-settings-earnings-tab",
            "payroll-settings-deductions-tab",
            "payroll-settings-worker-classes-tab",
            "payroll-settings-grades-tab",
            "payroll-settings-work-sites-tab",
        ):
            self.assertEqual(self.client.get(reverse(name), **HX).status_code, 200, name)
        self.create()
        for route in ("earnings", "deductions"):
            nav = self.client.get(reverse("pay-component-nav", kwargs={"route": route}), **HX)
            self.assertEqual(nav.status_code, 200)
            listing = self.client.get(reverse("pay-component-list", kwargs={"route": route}), **HX)
            self.assertEqual(listing.status_code, 200)
        listing = self.client.get(reverse("pay-component-list", kwargs={"route": "earnings"}), **HX)
        self.assertContains(listing, "Attendance Bonus")
        # Tabs share one page: each list has its own container id.
        self.assertContains(self.client.get(reverse("payroll-settings-earnings-tab"), **HX), 'id="listContainer_earnings"')
        self.assertContains(self.client.get(reverse("payroll-settings-deductions-tab"), **HX), 'id="listContainer_deductions"')
        self.create(route="deductions", name="Canteen")
        # ?tab= opens that tab (Back from a deduction lands on Deductions) and is remembered.
        opened = self.client.get(reverse("payroll-settings-tab-view") + "?tab=deductions", **HX)
        self.assertEqual(opened.context["active_target"], '[data-target="#payrollSettingsTabs2"]')
        again = self.client.get(reverse("payroll-settings-tab-view"), **HX)
        self.assertEqual(again.context["active_target"], '[data-target="#payrollSettingsTabs2"]')
        page = self.client.get(reverse("payroll-settings-view") + "?tab=deductions")
        self.assertContains(page, "payroll-settings-tab-view/?tab=deductions")
        tabs = self.client.get(reverse("payroll-settings-tab-view"), **HX)
        badges = [tab["badge"] for tab in tabs.context["tabs"]]
        self.assertEqual(badges[:2], [1, 1])  # counted before the tabs are opened
        self.assertContains(tabs, "Earnings")
        self.assertNotContains(tabs, "Allowances")

    def test_editor_save_publish_flow(self):
        component = self.create()
        editor = self.client.get(component.get_editor_url())
        self.assertEqual(editor.status_code, 200)
        self.assertContains(editor, "v1 · Draft")
        self.assertContains(editor, 'name="payslip_label" type="text" maxlength="100" class="oh-input w-100" value="Attendance Bonus"')  # a value, not a placeholder
        self.assertNotContains(editor, "Click Save to keep your changes")
        self.assertContains(editor, f'<a href="{reverse("payroll-settings-view")}?tab=earnings" class="oh-btn oh-btn--light-bkg" data-pc-leave>', count=2)  # header "Configuration" + save bar "Back"
        self.assertContains(editor, 'title="Publish v1 first."')  # New version waits for a published version
        start = rules.today().isoformat()
        kwargs = {"route": "earnings", "pk": component.pk, "version_no": 1}
        form = {
            "name": "Attendance Bonus",
            "freq": "RECURRING:EVERY_CYCLE",
            "disbursement_timing": "FOLLOW_CADENCE",
            "eligibility_mode": "ALL",
            "calc_value_mode": "FLAT",
            "calc_value_flat_amount": "1500",
            "tax_treatment": "TAXABLE",
            "show_on_payslip": "on",
            "display_order": "3",
            "effective_from": start,
            "dirty": "1",
        }
        # Structural re-render does not save anything.
        rendered = self.client.post(reverse("pay-component-render", kwargs=kwargs), {**form, "calc_value_mode": "FORMULA"}, **HX)
        self.assertContains(rendered, "Formula")
        self.assertContains(rendered, "Unsaved changes")
        details = component.versions.get()
        self.assertIsNone(details.effective_from)
        # Save stores it through the PATCH service.
        saved = self.client.post(reverse("pay-component-save", kwargs=kwargs), form, **HX)
        self.assertContains(saved, "All changes saved")
        # The version tabs above the form are re-sent with the saved date.
        self.assertContains(saved, 'id="pcHeader" class="flex flex-wrap items-center justify-between gap-3 mb-4" hx-swap-oob="true"')
        self.assertContains(saved, rules.today().strftime("%d %b %Y"))
        details.refresh_from_db()
        self.assertEqual(details.effective_from.isoformat(), start)
        self.assertEqual(details.calculations.get().flat_amount, 1500)
        confirm = self.client.post(reverse("pay-component-publish-confirm", kwargs=kwargs), {**form, "dirty": "0"}, **HX)
        self.assertContains(confirm, "applies to all eligible workers")
        published = self.client.post(reverse("pay-component-publish", kwargs=kwargs), **HX)
        self.assertIn("HX-Redirect", published.headers)
        details.refresh_from_db()
        self.assertEqual(details.publish_state, PublishState.PUBLISHED)
        # A published version only saves when it still passes the checks.
        bad = self.client.post(reverse("pay-component-save", kwargs=kwargs), {**form, "calc_value_flat_amount": "0"}, **HX)
        self.assertContains(bad, "Not saved")
        details.refresh_from_db()
        self.assertEqual(details.calculations.get().flat_amount, 1500)
        # New version: enabled once published, then disabled while the draft exists.
        new_version_url = reverse("pay-component-new-version", kwargs={"route": "earnings", "pk": component.pk})
        self.assertContains(self.client.get(component.get_editor_url()), f'data-pc-post="{new_version_url}"')
        created = self.client.post(new_version_url, **HX)
        self.assertIn("?v=2", created.headers["HX-Redirect"])
        self.assertContains(self.client.get(component.get_editor_url() + "?v=2"), "only one draft at a time")

    def test_condition_rows_and_ops(self):
        component = self.create(route="deductions", name="Canteen Recovery")
        kwargs = {"route": "deductions", "pk": component.pk, "version_no": 1}
        base = {"name": "Canteen Recovery", "freq": "RECURRING:MONTHLY", "eligibility_mode": "CONDITION", "calc_value_mode": "SLAB", "calc_value_slab_count": "0"}
        added = self.client.post(reverse("pay-component-render", kwargs=kwargs), {**base, "op_add_condition": "CTC"}, **HX)
        self.assertContains(added, 'name="cond_CTC_operator"')
        slab = self.client.post(reverse("pay-component-render", kwargs=kwargs), {**base, "op_add_slab": "value"}, **HX)
        self.assertContains(slab, 'name="calc_value_slab_0_from"')

    def test_create_modal_validation_and_toggle(self):
        response = self.client.get(reverse("pay-component-create", kwargs={"route": "earnings"}), **HX)
        self.assertContains(response, "Create draft")
        self.assertContains(response, 'placeholder="e.g. Night Shift Allowance"')
        deduction = self.client.get(reverse("pay-component-create", kwargs={"route": "deductions"}), **HX)
        self.assertContains(deduction, 'placeholder="e.g. Employee PF"')
        component = self.create()
        dup = self.client.post(reverse("pay-component-create", kwargs={"route": "earnings"}), {"name": "attendance bonus"}, **HX)
        self.assertContains(dup, "already uses this name")
        toggle_url = component.get_active_toggle_url()
        self.assertContains(self.client.get(toggle_url, **HX), "Deactivate")
        self.client.post(toggle_url, **HX)
        component.refresh_from_db()
        self.assertFalse(component.is_active)

    def test_masters_create(self):
        response = self.client.post(reverse("workerclass-create-view"), {"code": "blue", "name": "Blue Collar"}, **HX)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(WorkerClass.objects.entire().filter(code="BLUE", company_id=self.company).exists())
        self.client.post(reverse("grade-create-view"), {"code": "G1", "name": "Associate"}, **HX)
        self.assertTrue(Grade.objects.entire().filter(code="G1", company_id=self.company).exists())
        for name in ("workerclass-list", "grade-list", "work-site-list", "workerclass-nav", "grade-nav", "work-site-nav"):
            self.assertEqual(self.client.get(reverse(name), **HX).status_code, 200, name)

    def test_editor_breadcrumbs_read_as_sections(self):
        component = self.create(name="House Rent")
        page = self.client.get(component.get_editor_url())
        names = [crumb["name"] for crumb in page.context["breadcrumbs"]][1:]
        self.assertEqual(names, ["Payroll", "Configuration", "Earnings", "House Rent"])

    def test_draft_dates_allow_the_past(self):
        component = self.create(name="House Rent")
        page = self.client.get(component.get_editor_url())
        self.assertContains(page, 'name="effective_from" type="date" class="oh-input w-100" data-pc-date="from" value=')  # no min

    def test_editor_after_switching_company_goes_back_to_the_list(self):
        component = self.create(name="House Rent")
        other = make_company("Other Co")
        session = self.client.session
        session["selected_company"] = str(other.id)
        session.save()
        page = self.client.get(component.get_editor_url())
        self.assertRedirects(page, reverse("payroll-settings-view") + "?tab=earnings", fetch_redirect_response=False)

    def test_work_site_form_lists_the_company_states(self):
        from krew_company_onboarding.models import CompanyStateRegistration, GSTStateConfig
        from krew_payroll.models.pay_components import WorkSite

        karnataka = GSTStateConfig.objects.get(code="29")
        CompanyStateRegistration.objects.create(company=self.company, state=karnataka)
        form = self.client.get(reverse("work-site-create-view"), **HX)
        # Not the generic country/state picker (empty <select> filled by JS): real options.
        self.assertContains(form, 'name="site_state"')
        self.assertRegex(form.content.decode(), rf'<option value="{karnataka.pk}"\s*>\s*29 — Karnataka')
        self.assertNotContains(form, "Maharashtra")
        self.client.post(reverse("work-site-create-view"), {"site_state": karnataka.pk, "name": "Whitefield"}, **HX)
        site = WorkSite.objects.entire().get(name="Whitefield")
        self.assertEqual((site.state_id, site.company_id_id), (karnataka.pk, self.company.id))
        edit = self.client.get(reverse("work-site-update-view", kwargs={"pk": site.pk}), **HX)
        self.assertRegex(edit.content.decode(), rf'<option value="{karnataka.pk}"\s*selected')
        dup = self.client.post(reverse("work-site-create-view"), {"site_state": karnataka.pk, "name": "whitefield"}, **HX)
        self.assertContains(dup, "This site already exists in that state.")

    def test_state_rule_offers_only_the_company_states(self):
        from krew_company_onboarding.models import CompanyStateRegistration, GSTStateConfig

        ka, mh = GSTStateConfig.objects.get(code="29"), GSTStateConfig.objects.get(code="27")
        CompanyStateRegistration.objects.create(company=self.company, state=ka)
        component = self.create(route="deductions", name="State Levy")
        kwargs = {"route": "deductions", "pk": component.pk, "version_no": 1}
        base = {"name": "State Levy", "freq": "RECURRING:MONTHLY", "eligibility_mode": "CONDITION", "calc_value_mode": "FLAT"}
        rendered = self.client.post(reverse("pay-component-render", kwargs=kwargs), {**base, "op_add_condition": "STATE"}, **HX)
        self.assertContains(rendered, f'<option value="{ka.pk}"')
        self.assertNotContains(rendered, f'<option value="{mh.pk}"')
        # A state the company isn't registered in is refused on save too.
        saved = self.client.post(
            reverse("pay-component-save", kwargs=kwargs),
            {**base, "cond_fields": "STATE", "cond_STATE_values": [mh.pk], "calc_value_flat_amount": "100"},
            **HX,
        )
        self.assertContains(saved, "Not saved")
        self.assertFalse(rules.draft_of(component).conditions.exists())

    def test_employee_pickers_follow_company_and_rules(self):
        from krew_company_onboarding.models import CompanyStateRegistration, GSTStateConfig
        from krew_payroll.models.pay_components import EmployeePayProfile

        ka, mh = GSTStateConfig.objects.get(code="29"), GSTStateConfig.objects.get(code="27")
        for state in (ka, mh):
            CompanyStateRegistration.objects.create(company=self.company, state=state)
        anu = make_employee(company=self.company, email="anu@test.horilla", first_name="Anu")
        ravi = make_employee(company=self.company, email="ravi@test.horilla", first_name="Ravi")
        make_employee(company=self.company, email="noprofile@test.horilla", first_name="Nopro")
        make_employee(company=make_company("Elsewhere Co"), email="out@test.horilla", first_name="Outsider")
        EmployeePayProfile.objects.create(employee=anu, ctc_annual=600000, work_state=ka, payout_cadence="MONTHLY")
        EmployeePayProfile.objects.create(employee=ravi, ctc_annual=1200000, work_state=mh, payout_cadence="MONTHLY")

        component = self.create(name="Night Shift Allowance")
        kwargs = {"route": "earnings", "pk": component.pk, "version_no": 1}
        render = lambda extra: self.client.post(reverse("pay-component-render", kwargs=kwargs), {
            "name": "Night Shift Allowance", "freq": "RECURRING:EVERY_CYCLE", "calc_value_mode": "FLAT", **extra}, **HX).content.decode()
        def exclude_names(html):
            block = html[html.index('name="exclude_ids"'):]
            block = block[:block.index("</select>")]
            return {n for n in ("Anu", "Ravi", "Nopro", "Outsider") if n in block}

        # Include All: the whole company, never other companies.
        self.assertEqual(exclude_names(render({"eligibility_mode": "ALL"})), {"Anu", "Ravi", "Nopro"})
        # State = Karnataka: only Karnataka workers.
        by_state = render({"eligibility_mode": "CONDITION", "cond_fields": "STATE", "cond_STATE_values": [ka.pk]})
        self.assertEqual(exclude_names(by_state), {"Anu"})
        self.assertIn("1 employee matches the rules above.", by_state)
        # CTC above ₹10L.
        by_ctc = render({"eligibility_mode": "CONDITION", "cond_fields": "CTC", "cond_CTC_operator": "GT", "cond_CTC_value_from": "1000000"})
        self.assertEqual(exclude_names(by_ctc), {"Ravi"})
        # A rule with nothing picked yet doesn't empty the list.
        unfinished = render({"eligibility_mode": "CONDITION", "cond_fields": "GRADE"})
        self.assertEqual(exclude_names(unfinished), {"Anu", "Ravi", "Nopro"})
        # Specific employees: the include list is the company.
        specific = render({"eligibility_mode": "SPECIFIC"})
        block = specific[specific.index('name="include_ids"'):]
        block = block[:block.index("</select>")]
        self.assertIn("Anu", block)
        self.assertNotIn("Outsider", block)

    def test_summary_follows_the_form_and_dates_use_a_calendar(self):
        basic = self.create(name="Basic Pay")
        hra = self.create(name="House Rent")
        kwargs = {"route": "earnings", "pk": hra.pk, "version_no": 1}
        form = {
            "name": "House Rent",
            "freq": "RECURRING:EVERY_CYCLE",
            "eligibility_mode": "ALL",
            "calc_value_mode": "PERCENTAGE",
            "calc_value_rate": "40",
            "calc_value_bases": [str(basic.pk)],
            "tax_treatment": "TAXABLE",
            "dirty": "1",
        }
        # Switching to a percentage shows in the summary before it is saved.
        rendered = self.client.post(reverse("pay-component-render", kwargs=kwargs), form, **HX)
        self.assertContains(rendered, f"40% of {basic.code}")
        self.assertNotContains(rendered, "Flat amount (not set)")
        self.assertNotContains(rendered, "before publishing")
        self.assertNotContains(rendered, "Deactivate")
        self.assertContains(rendered, 'name="effective_from" type="date"')
        self.assertContains(rendered, 'name="effective_to" type="date"')
        # Any day works, mid-month included.
        mid = rules.today() + timedelta(days=12)
        saved = self.client.post(reverse("pay-component-save", kwargs=kwargs), {**form, "effective_from": mid.isoformat()}, **HX)
        self.assertContains(saved, "All changes saved")
        self.assertEqual(rules.draft_of(hra).effective_from, mid)


class PayProfileTabTests(PayComponentUITests):
    def test_pay_profile_save_and_state_check(self):
        from django.apps import apps

        from krew_payroll.models.pay_components import EmployeePayProfile, WorkSite

        GSTStateConfig = apps.get_model("krew_company_onboarding", "GSTStateConfig")
        ka = GSTStateConfig.objects.get(code="29")
        mh = GSTStateConfig.objects.get(code="27")
        from krew_company_onboarding.models import CompanyStateRegistration

        for state in (ka, mh):  # Company Setup → Place of Work
            CompanyStateRegistration.objects.create(company=self.company, state=state)
        site = WorkSite.objects.create(company_id=self.company, state=ka, name="Hosur Plant")
        url = reverse("employee-pay-profile-tab", kwargs={"pk": self.emp.pk})
        self.assertContains(self.client.get(url, **HX), "Pay profile")
        bad = self.client.post(url, {"ctc_annual": "480000", "payout_cadence": "MONTHLY", "work_state": mh.pk, "work_site": site.pk}, **HX)
        self.assertContains(bad, "not in the selected work state")
        ok = self.client.post(url, {"ctc_annual": "480000", "payout_cadence": "WEEKLY", "employment_type": "PAYROLL", "work_state": ka.pk, "work_site": site.pk}, **HX)
        self.assertContains(ok, "Pay profile saved")
        profile = EmployeePayProfile.objects.entire().get(employee=self.emp)
        self.assertEqual((profile.ctc_monthly, profile.company_id), (40000, self.company))

