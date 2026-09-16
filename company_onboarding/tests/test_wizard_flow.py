from datetime import date
from decimal import Decimal

from django.test import TestCase

from horilla.horilla_middlewares import _thread_locals
from horilla.testkit import CompanyFilterTestMixin, make_company, make_employee, make_user

from company_onboarding.models import GSTStateConfig


class WizardFlowTests(CompanyFilterTestMixin, TestCase):
    def setUp(self):
        # horilla.decorators.login_required requires the logged-in user to
        # have a linked Employee (request.user.employee_get) — a bare
        # make_user() alone gets silently redirected to /login/. See
        # base.management.commands.createhorillauser for the same
        # user+Employee pairing this mirrors.
        self.user = make_user("ventura_admin", is_superuser=True)
        make_employee(
            company=make_company("Ventura HQ"),
            email="ventura_admin@test.horilla",
            user=self.user,
        )
        self.client.force_login(self.user)
        # state is a ModelChoiceField now (FK to GSTStateConfig) -- the
        # form posts its pk, not the GST code string.
        self.maharashtra_state, _ = GSTStateConfig.objects.get_or_create(
            code="27", defaults={"name": "Maharashtra"}
        )
        # A logged-in user whose own Employee has a company link otherwise
        # gets CompanyMiddleware's own-company-resolution fallback applied
        # to their session — fine for company-scoped screens, wrong here,
        # since Company Setup wizards work ACROSS company boundaries by
        # design. Forcing "all" here is what a real user would do via the
        # company switcher before this kind of cross-company admin work.
        session = self.client.session
        session["selected_company"] = "all"
        session.save()

    def tearDown(self):
        # HorillaModel.save() reads _thread_locals.request.user to
        # auto-populate created_by. That's a plain threading.local, not
        # reset by TestCase's per-test transaction rollback, so a request
        # made via self.client here would otherwise leak a (soon
        # rolled-back) user reference into the next test's factory calls,
        # causing a created_by FK violation there instead of here.
        # CompanyFilterTestMixin.tearDown() (via super()) resets the
        # separate selected-company contextvar for the same class of
        # reason — company_onboarding's own models don't use
        # HorillaCompanyManager (see models.py's Architecture note), but
        # other company-scoped models elsewhere in the app still do, and
        # this keeps that contextvar from leaking across test methods too.
        _thread_locals.request = None
        super().tearDown()

    def _identity_payload(self, **overrides):
        payload = {
            "company": "Acme Client",
            "address": "1 Test St",
            "country": "India",
            "state": "Karnataka",
            "city": "Bengaluru",
            "zip": "560001",
        }
        payload.update(overrides)
        return payload

    def test_draft_with_only_identity_fields_succeeds(self):
        resp = self.client.post(
            "/company-onboarding/create/",
            {**self._identity_payload(), "action": "draft"},
        )
        self.assertEqual(resp.status_code, 302)

    def test_next_blocks_on_missing_state_and_contact(self):
        resp = self.client.post(
            "/company-onboarding/create/",
            {
                **self._identity_payload(),
                "legal_name": "Acme Pvt Ltd",
                "tax_country": "INDIA",
                "pan": "ABCDE1234F",
                "currency": "INR",
                "action": "next",
            },
        )
        # No state/POC rows submitted -> strict validation blocks, stays on
        # the page (200), doesn't redirect to step 2.
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Place of Work")

    def _next_payload_with_state_and_poc(self, **overrides):
        """
        A Next submission with the state/POC rows already present, so tests
        can isolate whichever OTHER mandatory field they're checking
        without also tripping the "at least one state/contact" errors.
        """
        payload = {
            **self._identity_payload(),
            "legal_name": "Acme Pvt Ltd",
            "state1-row_id": "",
            "state1-state": str(self.maharashtra_state.pk),
            "state1-gstin": "",
            "poc1-row_id": "",
            "poc1-designation": "HR Head",
            "poc1-name": "Jane Doe",
            "poc1-email": "jane@acme.test",
            "poc1-mobile": "9999999999",
            "action": "next",
        }
        payload.update(overrides)
        return payload

    def test_next_blocks_on_missing_pan_for_domestic_tax_country(self):
        resp = self.client.post(
            "/company-onboarding/create/",
            self._next_payload_with_state_and_poc(tax_country="INDIA"),  # no pan
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "PAN is required")

    def test_next_blocks_on_missing_foreign_tax_id_for_foreign_tax_country(self):
        resp = self.client.post(
            "/company-onboarding/create/",
            self._next_payload_with_state_and_poc(tax_country="FOREIGN"),  # no foreign_tax_id
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Foreign Tax ID is required")

    def test_next_blocks_on_incomplete_bank_details(self):
        resp = self.client.post(
            "/company-onboarding/create/",
            self._next_payload_with_state_and_poc(
                tax_country="INDIA",
                pan="ABCDE1234F",
                account_number="1234567890",
                bank_name="Test Bank",
                currency="INR",
                # ifsc_swift / account_holder_name / contact_number omitted
            ),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Bank details are required")

    def test_next_blocks_on_missing_contract_document(self):
        resp = self.client.post(
            "/company-onboarding/create/",
            self._next_payload_with_state_and_poc(
                tax_country="INDIA",
                pan="ABCDE1234F",
                account_number="1234567890",
                bank_name="Test Bank",
                ifsc_swift="TEST0001234",
                account_holder_name="Jane Doe",
                contact_number="9999999999",
                msa_reference_number="MSA-001",
                start_date="2026-01-01",
                billing_model="PER_HEAD",
                billing_value="500",
                # msa_document deliberately omitted
            ),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "MSA/Contract")

    def _create_company_via_draft(self):
        resp = self.client.post(
            "/company-onboarding/create/",
            {**self._identity_payload(), "action": "draft"},
        )
        company_id = resp["Location"].split("/")[-3]
        return int(company_id)

    def test_back_never_blocks_even_with_invalid_step1_data(self):
        company_id = self._create_company_via_draft()
        resp = self.client.post(
            f"/company-onboarding/{company_id}/step-1/",
            {
                **self._identity_payload(),
                "tax_country": "INDIA",  # PAN deliberately omitted
                "action": "back",
            },
        )
        self.assertEqual(resp.status_code, 302)

    def test_full_wizard_reaches_active(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        company_id = self._create_company_via_draft()

        pdf = SimpleUploadedFile("msa.pdf", b"%PDF-1.4 fake", content_type="application/pdf")
        resp = self.client.post(
            f"/company-onboarding/{company_id}/step-1/",
            {
                **self._identity_payload(),
                "legal_name": "Acme Pvt Ltd",
                "tax_country": "INDIA",
                "pan": "ABCDE1234F",
                "invoice_cycle": "MONTHLY",
                "account_number": "1234567890",
                "bank_name": "Test Bank",
                "ifsc_swift": "TEST0001234",
                "account_holder_name": "Jane Doe",
                "contact_number": "9999999999",
                "currency": "INR",
                "msa_reference_number": "MSA-001",
                "start_date": "2026-01-01",
                "billing_model": "PER_HEAD",
                "billing_value": "500",
                "msa_document": pdf,
                "state1-row_id": "",
                "state1-state": str(self.maharashtra_state.pk),
                "state1-gstin": "",
                "poc1-row_id": "",
                "poc1-designation": "HR Head",
                "poc1-name": "Jane Doe",
                "poc1-email": "jane@acme.test",
                "poc1-mobile": "9999999999",
                "action": "next",
            },
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], f"/company-onboarding/{company_id}/step-2/")

        resp = self.client.post(
            f"/company-onboarding/{company_id}/step-2/",
            {
                "signatory1-row_id": "",
                "signatory1-signatory_type": "VENTURA",
                "signatory1-name": "Som Naskar",
                "signatory1-designation": "Director",
                "signatory1-email": "som@ventura.test",
                "signatory1-is_enabled": "on",
                "action": "next",
            },
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], f"/company-onboarding/{company_id}/step-3/")

        from base.models import Company

        # Bank details are filled in but not yet VERIFIED -- Mark-as-Active
        # must still block (this is the actual, previously-missing gate).
        resp = self.client.post(
            f"/company-onboarding/{company_id}/step-3/", {"action": "mark_active"}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "verified")
        company = Company.objects.get(pk=company_id)
        self.assertNotEqual(company.status, "ACTIVE")

        from company_onboarding.models import CompanyBankDetails
        from company_onboarding.wizard_utils import get_bank_details

        bank_details = get_bank_details(company)
        bank_details.verification_status = CompanyBankDetails.VerificationStatus.VERIFIED
        bank_details.save(update_fields=["verification_status"])

        # Step 3 completeness (documents/branding) explicitly not required
        # for Active.
        resp = self.client.post(
            f"/company-onboarding/{company_id}/step-3/", {"action": "mark_active"}
        )
        self.assertEqual(resp.status_code, 302)

        company.refresh_from_db()
        self.assertEqual(company.status, "ACTIVE")

    def _activate_company(
        self,
        company_id,
        *,
        msa_reference_number="MSA-001",
        billing_value="500",
        msa_document=None,
        end_date=None,
    ):
        from django.core.files.uploadedfile import SimpleUploadedFile

        pdf = msa_document or SimpleUploadedFile(
            "msa.pdf", b"%PDF-1.4 fake", content_type="application/pdf"
        )
        payload = {
            **self._identity_payload(),
            "legal_name": "Acme Pvt Ltd",
            "tax_country": "INDIA",
            "pan": "ABCDE1234F",
            "invoice_cycle": "MONTHLY",
            "account_number": "1234567890",
            "bank_name": "Test Bank",
            "ifsc_swift": "TEST0001234",
            "account_holder_name": "Jane Doe",
            "contact_number": "9999999999",
            "currency": "INR",
            "msa_reference_number": msa_reference_number,
            "start_date": "2026-01-01",
            "billing_model": "PER_HEAD",
            "billing_value": billing_value,
            "msa_document": pdf,
            "state1-row_id": "",
            "state1-state": str(self.maharashtra_state.pk),
            "state1-gstin": "",
            "poc1-row_id": "",
            "poc1-designation": "HR Head",
            "poc1-name": "Jane Doe",
            "poc1-email": "jane@acme.test",
            "poc1-mobile": "9999999999",
            "action": "next",
        }
        if end_date is not None:
            payload["end_date"] = end_date
        self.client.post(f"/company-onboarding/{company_id}/step-1/", payload)

        # Mark-as-Active requires the bank account to be VERIFIED, not just
        # filled in (see wizard_utils.validate_for_active()) -- this helper
        # is shared by tests that don't care about the Cashfree flow itself,
        # so it sets the outcome directly via the ORM rather than mocking
        # verify_bank_account()/initiate_bank_transfer() end-to-end here.
        # Dedicated bank-verification tests exercise that flow separately.
        from company_onboarding.models import CompanyBankDetails
        from company_onboarding.wizard_utils import get_bank_details
        from base.models import Company

        bank_details = get_bank_details(Company.objects.get(pk=company_id))
        bank_details.verification_status = CompanyBankDetails.VerificationStatus.VERIFIED
        bank_details.save(update_fields=["verification_status"])

        self.client.post(
            f"/company-onboarding/{company_id}/step-2/",
            {
                "signatory1-row_id": "",
                "signatory1-signatory_type": "VENTURA",
                "signatory1-name": "Som Naskar",
                "signatory1-designation": "Director",
                "signatory1-email": "som@ventura.test",
                "signatory1-is_enabled": "on",
                "action": "next",
            },
        )
        self.client.post(
            f"/company-onboarding/{company_id}/step-3/", {"action": "mark_active"}
        )

    def _amend_contract(
        self,
        company_id,
        *,
        msa_reference_number="MSA-001",
        billing_value="500",
        msa_document=None,
        action="draft",
        start_date="2026-02-01",
    ):
        """
        Re-submits Step 1 with new contract terms for an already-Active
        company -- everything else stays as the identity payload requires,
        since Step1View re-validates the whole step even on a Draft save.
        msa_document is omitted by default -- amending an active contract's
        terms REQUIRES a freshly uploaded document (see Step1View.post()),
        so tests exercising a real amendment must pass one explicitly.
        start_date defaults to a month after _activate_company()'s
        hardcoded 2026-01-01 -- amending with the SAME start date as the
        current contract is itself invalid (a new contract can't start on
        or before the one it's replacing), so tests that don't care about
        exact date-continuity behavior still need a later date here.
        """
        payload = {
            **self._identity_payload(),
            "legal_name": "Acme Pvt Ltd",
            "tax_country": "INDIA",
            "pan": "ABCDE1234F",
            "invoice_cycle": "MONTHLY",
            "msa_reference_number": msa_reference_number,
            "start_date": start_date,
            "billing_model": "PER_HEAD",
            "billing_value": billing_value,
            "action": action,
        }
        if msa_document is not None:
            payload["msa_document"] = msa_document
        return self.client.post(f"/company-onboarding/{company_id}/step-1/", payload)

    def _amendment_document(self, name="amendment.pdf"):
        from django.core.files.uploadedfile import SimpleUploadedFile

        return SimpleUploadedFile(name, b"%PDF-1.4 fake amendment", content_type="application/pdf")

    def test_amending_an_active_contract_terminates_old_and_creates_new(self):
        from company_onboarding.models import CompanyContract

        company_id = self._create_company_via_draft()
        self._activate_company(company_id, billing_value="500")

        original = CompanyContract.objects.get(company_id=company_id, status="ACTIVE")
        self._amend_contract(
            company_id, billing_value="750", msa_document=self._amendment_document()
        )

        original.refresh_from_db()
        self.assertEqual(original.status, "TERMINATED")
        self.assertEqual(
            CompanyContract.objects.filter(company_id=company_id).count(), 2
        )
        new_active = CompanyContract.objects.get(
            company_id=company_id, status="ACTIVE"
        )
        self.assertNotEqual(new_active.pk, original.pk)
        self.assertEqual(new_active.billing_value, Decimal("750.00"))

    def test_amending_with_same_msa_reference_number_renames_the_terminated_row(self):
        from company_onboarding.models import CompanyContract

        company_id = self._create_company_via_draft()
        self._activate_company(
            company_id, msa_reference_number="MSA-001", billing_value="500"
        )
        original = CompanyContract.objects.get(company_id=company_id, status="ACTIVE")

        # Amendment resubmits the SAME reference number -- msa_reference_
        # number is globally unique even across TERMINATED rows, so this
        # would collide with the outgoing row unless it gets renamed.
        self._amend_contract(
            company_id,
            msa_reference_number="MSA-001",
            billing_value="750",
            msa_document=self._amendment_document(),
        )

        original.refresh_from_db()
        self.assertNotEqual(original.msa_reference_number, "MSA-001")
        self.assertIn(f"#{original.pk}", original.msa_reference_number)
        new_active = CompanyContract.objects.get(
            company_id=company_id, status="ACTIVE"
        )
        self.assertEqual(new_active.msa_reference_number, "MSA-001")

    def test_amending_with_a_new_msa_reference_number_leaves_the_old_one_untouched(
        self,
    ):
        from company_onboarding.models import CompanyContract

        company_id = self._create_company_via_draft()
        self._activate_company(
            company_id, msa_reference_number="MSA-001", billing_value="500"
        )
        original = CompanyContract.objects.get(company_id=company_id, status="ACTIVE")

        self._amend_contract(
            company_id,
            msa_reference_number="MSA-002",
            billing_value="750",
            msa_document=self._amendment_document(),
        )

        original.refresh_from_db()
        self.assertEqual(original.msa_reference_number, "MSA-001")
        self.assertEqual(original.status, "TERMINATED")

    def test_resubmitting_unchanged_contract_terms_does_not_create_a_new_row(self):
        from company_onboarding.models import CompanyContract

        company_id = self._create_company_via_draft()
        self._activate_company(
            company_id, msa_reference_number="MSA-001", billing_value="500"
        )
        original = CompanyContract.objects.get(company_id=company_id, status="ACTIVE")

        # Same terms as activation, no document re-uploaded either -- the
        # wizard resubmits this whole section on every save even when
        # nothing changed. Since terms are unchanged, the missing-document
        # requirement never even applies here.
        self._amend_contract(
            company_id, msa_reference_number="MSA-001", billing_value="500"
        )

        self.assertEqual(
            CompanyContract.objects.filter(company_id=company_id).count(), 1
        )
        original.refresh_from_db()
        self.assertEqual(original.status, "ACTIVE")

    def test_amending_without_a_new_document_is_blocked(self):
        from django.contrib.messages import get_messages

        from company_onboarding.models import CompanyContract

        company_id = self._create_company_via_draft()
        self._activate_company(company_id, billing_value="500")
        original = CompanyContract.objects.get(company_id=company_id, status="ACTIVE")
        original_document_name = original.msa_document.name

        # Real term change (500 -> 750) but no msa_document attached.
        resp = self._amend_contract(company_id, billing_value="750")

        # This is a hard block (like tax_country_locked), not a relaxed-on-
        # Draft check -- it aborts the request immediately by re-rendering
        # Step 1 (200), same as every other request-level validation
        # failure in this view. It must NOT redirect forward/onward
        # regardless of action, which is exactly the bug being guarded
        # against here.
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "requires uploading")
        messages = [str(m) for m in get_messages(resp.wsgi_request)]
        self.assertTrue(any("requires uploading" in m for m in messages))
        self.assertEqual(
            CompanyContract.objects.filter(company_id=company_id).count(), 1
        )
        original.refresh_from_db()
        self.assertEqual(original.status, "ACTIVE")
        self.assertEqual(original.billing_value, Decimal("500.00"))
        self.assertEqual(original.msa_document.name, original_document_name)

    def test_clicking_next_with_a_blocked_amendment_does_not_advance_to_step2(self):
        """
        Regression test: the missing-document block previously only queued
        a messages.error() toast with no effect on control flow, so
        clicking Next silently redirected to Step 2 anyway despite the
        error being shown.
        """
        company_id = self._create_company_via_draft()
        self._activate_company(company_id, billing_value="500")

        resp = self._amend_contract(company_id, billing_value="750", action="next")

        self.assertEqual(resp.status_code, 200)
        self.assertNotEqual(resp.get("Location"), f"/company-onboarding/{company_id}/step-2/")
        self.assertContains(resp, "requires uploading")

    def test_amending_with_a_new_document_replaces_the_old_one(self):
        from company_onboarding.models import CompanyContract

        company_id = self._create_company_via_draft()
        self._activate_company(company_id, billing_value="500")
        original = CompanyContract.objects.get(company_id=company_id, status="ACTIVE")
        original_document_name = original.msa_document.name

        self._amend_contract(
            company_id, billing_value="750", msa_document=self._amendment_document()
        )

        new_active = CompanyContract.objects.get(
            company_id=company_id, status="ACTIVE"
        )
        self.assertTrue(new_active.msa_document)
        self.assertNotEqual(new_active.msa_document.name, original_document_name)

    def test_amending_backfills_old_contracts_end_date_when_not_set(self):
        from company_onboarding.models import CompanyContract

        company_id = self._create_company_via_draft()
        # end_date deliberately left unset.
        self._activate_company(company_id, billing_value="500")
        original = CompanyContract.objects.get(company_id=company_id, status="ACTIVE")
        self.assertIsNone(original.end_date)

        self._amend_contract(
            company_id,
            billing_value="750",
            start_date="2026-03-15",
            msa_document=self._amendment_document(),
        )

        original.refresh_from_db()
        self.assertEqual(original.end_date, date(2026, 3, 14))

    def test_amending_rejects_a_start_date_that_leaves_a_gap_after_old_end_date(self):
        from django.contrib.messages import get_messages

        from company_onboarding.models import CompanyContract

        company_id = self._create_company_via_draft()
        self._activate_company(
            company_id, billing_value="500", end_date="2026-01-31"
        )
        original = CompanyContract.objects.get(company_id=company_id, status="ACTIVE")

        # Old contract ends 2026-01-31 -> new one must start 2026-02-01.
        # This leaves a gap by starting a week later instead.
        resp = self._amend_contract(
            company_id,
            billing_value="750",
            start_date="2026-02-08",
            msa_document=self._amendment_document(),
        )

        self.assertEqual(resp.status_code, 200)
        messages = [str(m) for m in get_messages(resp.wsgi_request)]
        self.assertTrue(any("2026-02-01" in m for m in messages))
        original.refresh_from_db()
        self.assertEqual(original.status, "ACTIVE")
        self.assertEqual(
            CompanyContract.objects.filter(company_id=company_id).count(), 1
        )

    def test_amending_rejects_a_start_date_that_overlaps_old_end_date(self):
        from django.contrib.messages import get_messages

        from company_onboarding.models import CompanyContract

        company_id = self._create_company_via_draft()
        self._activate_company(
            company_id, billing_value="500", end_date="2026-01-31"
        )
        original = CompanyContract.objects.get(company_id=company_id, status="ACTIVE")

        # Starting ON the old end date overlaps it by a day.
        resp = self._amend_contract(
            company_id,
            billing_value="750",
            start_date="2026-01-31",
            msa_document=self._amendment_document(),
        )

        self.assertEqual(resp.status_code, 200)
        messages = [str(m) for m in get_messages(resp.wsgi_request)]
        self.assertTrue(any("2026-02-01" in m for m in messages))
        original.refresh_from_db()
        self.assertEqual(original.status, "ACTIVE")

    def test_amending_succeeds_when_start_date_exactly_follows_old_end_date(self):
        from company_onboarding.models import CompanyContract

        company_id = self._create_company_via_draft()
        self._activate_company(
            company_id, billing_value="500", end_date="2026-01-31"
        )
        original = CompanyContract.objects.get(company_id=company_id, status="ACTIVE")

        self._amend_contract(
            company_id,
            billing_value="750",
            start_date="2026-02-01",
            msa_document=self._amendment_document(),
        )

        original.refresh_from_db()
        self.assertEqual(original.status, "TERMINATED")
        self.assertEqual(original.end_date, date(2026, 1, 31))  # untouched
        new_active = CompanyContract.objects.get(
            company_id=company_id, status="ACTIVE"
        )
        self.assertEqual(new_active.start_date, date(2026, 2, 1))

    def test_amending_rejects_a_start_date_on_or_before_the_old_contracts_own_start(
        self,
    ):
        from django.contrib.messages import get_messages

        from company_onboarding.models import CompanyContract

        company_id = self._create_company_via_draft()
        # end_date deliberately left unset -- the current contract started
        # 2026-01-01 (see _activate_company()).
        self._activate_company(company_id, billing_value="500")

        resp = self._amend_contract(
            company_id,
            billing_value="750",
            start_date="2026-01-01",
            msa_document=self._amendment_document(),
        )

        self.assertEqual(resp.status_code, 200)
        messages = [str(m) for m in get_messages(resp.wsgi_request)]
        self.assertTrue(any("must be after" in m for m in messages))
        self.assertEqual(
            CompanyContract.objects.filter(company_id=company_id).count(), 1
        )

    def test_step1_get_shows_active_contract_readonly_with_a_blank_amendment_form(
        self,
    ):
        """
        Once the company is Active and a contract exists, GET /step-1/
        must NOT pre-fill the contract form with the current contract's
        values -- editing it directly (instead of via the "Add New
        Contract" toggle) would look like an in-place edit, but a real
        submission actually creates a new row (see Step1View.post()).
        """
        company_id = self._create_company_via_draft()
        self._activate_company(
            company_id, msa_reference_number="MSA-001", billing_value="500"
        )

        resp = self.client.get(f"/company-onboarding/{company_id}/step-1/")

        self.assertTrue(resp.context["amending_active_contract"])
        self.assertEqual(
            resp.context["active_contract"].msa_reference_number, "MSA-001"
        )
        contract_form = resp.context["contract_form"]
        self.assertIsNone(contract_form.initial.get("msa_reference_number"))
        self.assertContains(resp, "MSA-001")  # shown in the read-only summary
        self.assertContains(resp, "Add New Contract")

    def test_step1_get_hides_penny_drop_controls_once_bank_account_is_verified(self):
        """
        Regression test: the "Initiate Penny Drop" button and the "Amount
        credited" confirmation input are only meaningful before
        verification -- once verification_status is VERIFIED, neither
        should still render (they previously kept showing regardless,
        since that block only checked the latest attempt's own
        drop_status, never the account's overall verification_status).
        """
        company_id = self._create_company_via_draft()
        self._activate_company(company_id)  # sets verification_status=VERIFIED

        resp = self.client.get(f"/company-onboarding/{company_id}/step-1/")

        self.assertContains(resp, "Bank account verified.")
        self.assertNotContains(resp, "Initiate Penny Drop")
        self.assertNotContains(resp, "Amount credited")

    def test_mark_active_reblocks_when_a_required_field_is_cleared_after_next(self):
        """
        Mark-as-Active is an independent re-check, not a "Next already
        passed" shortcut — clearing a required field directly (bypassing
        the wizard) must still block it.
        """
        from django.core.files.uploadedfile import SimpleUploadedFile

        company_id = self._create_company_via_draft()
        pdf = SimpleUploadedFile("msa.pdf", b"%PDF-1.4 fake", content_type="application/pdf")
        self.client.post(
            f"/company-onboarding/{company_id}/step-1/",
            {
                **self._identity_payload(),
                "legal_name": "Acme Pvt Ltd",
                "tax_country": "INDIA",
                "pan": "ABCDE1234F",
                "invoice_cycle": "MONTHLY",
                "account_number": "1234567890",
                "bank_name": "Test Bank",
                "ifsc_swift": "TEST0001234",
                "currency": "INR",
                "msa_reference_number": "MSA-001",
                "start_date": "2026-01-01",
                "billing_model": "PER_HEAD",
                "billing_value": "500",
                "msa_document": pdf,
                "state1-row_id": "",
                "state1-state": str(self.maharashtra_state.pk),
                "state1-gstin": "",
                "poc1-row_id": "",
                "poc1-designation": "HR Head",
                "poc1-name": "Jane Doe",
                "poc1-email": "jane@acme.test",
                "poc1-mobile": "9999999999",
                "action": "next",
            },
        )
        self.client.post(
            f"/company-onboarding/{company_id}/step-2/",
            {
                "signatory1-row_id": "",
                "signatory1-signatory_type": "VENTURA",
                "signatory1-name": "Som Naskar",
                "signatory1-designation": "Director",
                "signatory1-email": "som@ventura.test",
                "signatory1-is_enabled": "on",
                "action": "next",
            },
        )

        from base.models import Company

        company = Company.objects.get(pk=company_id)
        company.legal_name = ""
        company.save(update_fields=["legal_name"])

        resp = self.client.post(
            f"/company-onboarding/{company_id}/step-3/", {"action": "mark_active"}
        )
        self.assertEqual(resp.status_code, 200)
        company.refresh_from_db()
        self.assertNotEqual(company.status, "ACTIVE")
