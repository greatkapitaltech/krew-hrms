# krew_company_onboarding — developer guide

"Company Setup" — the client-company onboarding/compliance-gate module (Ventura Admin/HR onboarding a client of Great Kapital onto Krew). This file documents the conventions this app follows and the gotchas that aren't obvious from reading a single file in isolation. Read it before adding a model, field, form, or view here.

## 1. Where data lives — split with `base.Company`

- The **core, singular** compliance fields (legal name, PAN, tax country, status, invoice cycle, ...) live directly on `base.Company` (`base/models.py`), not in this app. `base.Company` is the tenant-identity model every other app already depends on — putting the compliance fields there avoids an always-needed join.
- **Everything repeatable / child-table** (state registrations, POC contacts, bank details + verification, contracts, signatories, branded templates, documents, deactivation records) lives here in `krew_company_onboarding/models.py`, FK'd to `base.Company`.
- **Dependency direction is one-way: `krew_company_onboarding → base`, never the reverse.** `base` must never import from this app. (This was violated once — `base/models.py` used to import `EncryptedCharField`/`hash_value` from here — and was fixed by moving those helpers into `base/` itself; see §4.)
- If you need "who approves payroll for this company," don't add an FK from `Company` to `CompanyPOCContact` — that would create the backwards dependency. Use `company.poc_contacts.filter(is_payroll_approver=True).first()` instead.

## 2. Naming history — don't be surprised by mismatched names

This app was renamed from `company_onboarding` to `krew_company_onboarding`. Two things were deliberately **not** renamed and you should not "fix" them:

- **Templates still live under `templates/company_onboarding/`**, not `templates/krew_company_onboarding/`. This is intentional — Django's template loader resolves by this path string across the whole project, and changing it has no benefit worth the churn.
- **DB tables are `krew_company_onboarding_<model>`** (physically renamed via `migrations/0022`–`0024`) — but two other apps still literally construct that string, so don't hardcode a different prefix if you add raw SQL.
- `krew_company_onboarding/model_fields.py` and `krew_company_onboarding/encryption.py` are **compatibility shims** (a few lines re-exporting from `base.model_fields` / `base.encryption`) — kept only because two already-applied migrations have the old `krew_company_onboarding.model_fields.EncryptedCharField` import path frozen into their generated code. **Do not delete these shims**, and do not add new code to them — new code imports from `base.*` directly (§4).

## 3. Managers — plain `models.Manager()`, not `HorillaCompanyManager`

Every model in this file uses `objects = models.Manager()` (or the implicit default), **not** `HorillaCompanyManager`, even though these look like typical company-scoped child records.

**Why:** `HorillaCompanyManager` filters querysets by the *acting user's own currently-selected company*. These models are the opposite — they're Ventura staff's own administrative records *about* client companies, edited by Ventura Admin/HR who by design operate *across* company boundaries (one Ventura Admin onboards many different client companies). Company-scoping here would silently hide a company's own state registrations/POC contacts/etc. from the very admin editing that company, the moment their own `Employee` record happens to be linked to any company. This was confirmed by hitting it directly in testing — don't reintroduce it.

`base.Company` itself uses the same plain-manager pattern for the identical reason.

## 4. Shared helpers live in `base/`, not here

`base/encryption.py`, `base/model_fields.py`, `base/validators.py` are the canonical, global locations for:

- `EncryptedCharField` (`base/model_fields.py`) — transparent encrypt-on-write/decrypt-on-read `CharField`.
- `encrypt_value` / `decrypt_value` / `hash_value` / `mask_value` (`base/encryption.py`).
- `pan_validator`, `gstin_validator`, `phone_number_validator` (`base/validators.py`).

**Import from `base.*`, not from anywhere in this app** — the versions under `krew_company_onboarding/` are compatibility shims only (§2).

**If you add a new field that needs both encryption AND uniqueness** (e.g. another government ID), follow the `Company.pan` / `Company.pan_hash` pattern, not a naive `unique=True` on an `EncryptedCharField`:

1. Field itself: `EncryptedCharField(...)`.
2. A companion `<field>_hash = models.CharField(max_length=64, editable=False)` — a deterministic HMAC "blind index" via `hash_value()`.
3. `UniqueConstraint` on the `_hash` column, not the encrypted column — Fernet ciphertext is non-deterministic (random IV), so two rows with the *same* plaintext never produce the same ciphertext, and a uniqueness check against the ciphertext column would never catch a real duplicate.
4. Set the hash in `clean()`/`save()` whenever the plaintext field changes.

**If you add a phone number field**, use `base.validators.phone_number_validator` (strict 10 digits) for anything company-onboarding-specific, not `employee.models.phone_validator` (loose, 7–20 chars, accepts `+`) — that one is for general employee-facing phone fields elsewhere in the codebase.

## 5. Choice fields — `TextChoices`, not plain tuples

Every choice field in *this app's* models uses Django's `models.TextChoices` pattern:

```python
class SignatoryType(models.TextChoices):
    VENTURA = "ventura", _("Ventura")
    CLIENT = "client", _("Client")

signatory_type = models.CharField(max_length=20, choices=SignatoryType.choices)
```

This is a deliberate, contained convention for this app's own new models — `base.Company`'s own choice fields (`STATUS_CHOICES`, `TAX_COUNTRY_CHOICES`, etc.) stay plain tuples to match the rest of `base/models.py`, which predates this pattern. Match whichever file you're editing; don't convert `base.Company`'s tuples to `TextChoices` as a drive-by change.

## 6. The wizard's validation tiers — and a real gotcha

Step 1 (`krew_company_onboarding/cbv/wizard.py::Step1View`) has **four** distinct validation tiers, not two:

1. **Save as Draft** — relaxed. Only the "1.1 Identity" subset (`CompanyIdentityForm`) is required.
2. **Next** — strict for the *current* step's fields.
3. **Back** — same as Draft, never blocks.
4. **Mark as Active** — an *independent* re-validation of everything mandatory across Steps 1–2, via `wizard_utils.validate_for_active()`. Re-derives pass/fail from current DB state every time — never assume "Next already passed" is good enough.

**The gotcha:** `_StrictToggleMixin` (`forms.py`) toggles `strict=` by setting `field.required = False` on Draft — **it never sets `field.required = True` on strict.** Whether a field is actually enforced on Next/Mark-as-Active depends entirely on the underlying model field's own `blank=` attribute. This bit us once already: a whole set of fields (tax/bank/contract) was silently never enforced on Next because every relevant model field was `blank=True` and the mixin was assumed (wrongly) to promote them to required.

**If you add a new field that must be mandatory on Next or Mark-as-Active:** don't rely on `strict=True` alone. Add an explicit check reading `cleaned_data`/the saved instance directly — either into `Step1View.post()`'s `extra_errors` list (for Next) or into `wizard_utils.validate_for_active()`'s blocker list (for Mark-as-Active), matching the existing pattern for tax/bank/contract fields there. Test both tiers explicitly — a passing "Next" test does not prove the field is actually required.

## 7. Contract amendment — don't allow direct edits to an active contract

If a company already has an active `CompanyContract`, Step 1 must **not** let it be edited in place — it opens a new contract (terminate-old/create-new), never a silent overwrite of contract terms. Rules baked into `Step1View.post()`, all deliberate:

- A new document upload is **mandatory** for a real amendment (changed terms) — missing document hard-aborts the whole request (`return render(...)` before any section saves), the same pattern as the pre-existing `tax_country_locked` check. Don't downgrade this to a `messages.error()` that lets the request continue.
- New contract dates must have **zero gap and zero overlap** with the outgoing contract: new `start_date` must be exactly `old.end_date + 1 day`, no earlier and no later.
- `msa_reference_number` is globally unique even across `TERMINATED` rows (by design — see the existing uniqueness test). If the new contract reuses the same reference number, the outgoing row is renamed to `"{old} [superseded #{pk}]"` (using its own guaranteed-unique pk) to avoid a collision — only when a collision would actually happen.

## 8. Bank verification (penny drop) — service layer, not inline in views

`services/bank_verification.py` wraps Cashfree. `initiate_penny_drop()` / `confirm_penny_drop()` are the only entry points views should call — don't call `services/cashfree_client.py` directly from a view. The `CompanyBankVerification.verified_attempt` FK on `CompanyBankDetails` records which specific attempt succeeded — check `verification_status`, not `drop_status`, when deciding whether to show "Initiate"/"Confirm" controls in a template (a past bug showed the controls again after verification because it checked the wrong field).

## 9. Permissions — two different mechanisms, on purpose

- **`base.Company` permissions** (`view_company`, `add_company`, `change_company`, `change_status_company`): Ventura Admin now gets these via full `base` app access in `base/signals.py`'s `_DEFAULT_HRMS_GROUPS`. Ventura HR gets `view_company` only, granted directly in `krew_company_onboarding/signals.py` (see below) because `_DEFAULT_HRMS_GROUPS`'s `app_actions` is app-level, not model-level, and can't isolate to just `Company` within the much larger `base` app.
- **Model-level grants that can't be expressed via `_DEFAULT_HRMS_GROUPS`** (e.g. Ventura HR's `add`/`change` on *only* `CompanyBrandedTemplate`/`CompanyDocument`, not the rest of this app) are granted directly in `krew_company_onboarding/signals.py`'s `post_migrate` receivers. **If you add a new model that needs a narrower-than-whole-app permission slice for some group, extend `signals.py` the same way** — don't try to force it through `_DEFAULT_HRMS_GROUPS`.
- Both signal handlers are additive-only (`group.permissions.add(...)`, never remove) and idempotent, so they safely re-run on every `migrate` — see the module docstring in `signals.py` for why `post_migrate` (not a one-off management command) is the right hook.

## 10. Repeatable rows — prefix-indexed forms, not Django formsets

Place of Work states, POC Contacts, and Signatories use `wizard_utils.discover_row_indices()` + per-row `ModelForm(data, prefix=f"{prefix}{index}")`, not a Django formset — deliberately, since rows have multiple correlated fields and no formset management-form bookkeeping is wanted. If you add another repeatable field group, follow this same prefix-indexed pattern rather than introducing a formset alongside it.

## 11. Tests

- Live in `krew_company_onboarding/tests/` (not this app's mirror of `base/tests/`).
- Use `horilla.testkit.factories` (`make_company`, `make_user`, `make_employee`, ...) over building model graphs by hand.
- `test_wizard_flow.py`'s `_activate_company()` helper sets `verification_status=VERIFIED` via direct ORM write — Mark-as-Active requires a verified bank account, and the test helper bypasses the real Cashfree stub flow deliberately for speed.
- When adding a new mandatory field (§6), write both a "Next blocks without it" test and a "Mark-as-Active blocks without it" test — they exercise different code paths and one passing does not imply the other does.
