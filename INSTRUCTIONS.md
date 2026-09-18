# Building a new app in this codebase

General conventions for adding a new first-party Django app to Krew HRMS. Where a rule was learned the hard way on a past app, the reasoning is included so it isn't reintroduced by accident.

## 1. App layout

Follow the shape every existing first-party app already uses (see `CLAUDE.md`'s "App layout" section):

```
<app>/
  models.py         # extends HorillaModel; HorillaCompanyManager by default (see §3)
  forms.py
  views.py + urls.py            # classic FBV/CBV, HTMX-driven
  cbv/                          # class-based views on the shared generic CBV framework
  services/                     # external-integration wrappers (payment gateways, etc.)
  templates/<app>/, static/<app>/, templatetags/
  tests/                        # colocated per app, not a top-level tests/ dir
  migrations/, admin.py, apps.py, signals.py, sidebar.py/dashboard.py
```

Register the app the way existing apps do — self-registering `urlpatterns` from `apps.py::ready()`, not a manual edit to `horilla/urls.py`:

```python
def ready(self):
    from horilla.urls import urlpatterns
    urlpatterns.append(path("your-app/", include("your_app.urls")))
```

Add the new app to `INSTALLED_APPS` and `SIDEBARS` in `horilla/settings/base.py`, and to `SMOKE_LABELS`/`UNIT_LABELS` in the `Makefile` so CI actually exercises it.

## 2. Global helpers go in `base/`, not duplicated per app

`base` is the one app every other app is already allowed to depend on. Any helper that isn't genuinely specific to one app's domain — encryption, custom model fields, generic validators, cross-app model methods — belongs in `base/`, not copied or reinvented inside your new app.

Concrete precedent: custom encryption helpers, an encrypted model field type, and a set of format validators (PAN, GSTIN, 10-digit phone) were originally built inside one app's own module, then moved to `base/model_fields.py`, `base/encryption.py`, `base/validators.py` once `base.Company` itself needed the encrypted field type for one of its own fields. That created a backwards dependency (`base` importing from a non-`base` app) — caught and fixed by promoting the helpers to `base`, not by threading the dependency the other way.

**Rule of thumb:** if you're about to import something from a sibling app that isn't `base`, stop — either that thing belongs in `base`, or your dependency direction is backwards.

**If a helper ever needs to move and something already references its old module path** (most commonly: a custom model field class referenced by an already-applied migration's frozen `field=some_app.some_module.SomeField(...)` code) — leave a **compatibility shim** at the old location (a few lines re-exporting the real thing from its new home) rather than editing historical migration files. Migrations replay a shim exactly like the original, since it's the literal same class object; verify with `python manage.py makemigrations --check` (should report "No changes detected") before considering it done.

**Changing something that already lives in `base/` is a different risk profile than changing your own app.** Every app is free to import from `base`, and `base` has no record of who actually does — a change there (renaming/removing a field or function, tightening a validator's regex, altering `EncryptedCharField`'s behavior, restructuring `base/signals.py`'s permission groups) can silently break an app you weren't even thinking about. Before editing anything in `base/`:

1. Grep the whole repo for usages of what you're about to change, not just the app you're currently working in.
2. Run the test suites of every app that showed up in that grep, not only your own app's tests.
3. If you're only *adding* something new to `base/`, that's low-risk (nothing depends on it yet). The risk is specifically in changing or removing something that already exists there.

## 3. Managers: `HorillaCompanyManager` is the default — know when to deviate

Company-scoped models should use `HorillaCompanyManager` as their default manager (`base/horilla_company_manager.py`) — that's what makes multi-tenancy work automatically for that model.

**The one legitimate exception:** a model that represents staff's own administrative data *about* companies, edited by users who by design operate *across* company boundaries (not data that belongs to one tenant). `base.Company` itself, and a handful of models like it, use a plain `models.Manager()` for exactly this reason — `HorillaCompanyManager` would filter by the *acting user's own* selected company, silently hiding a company's own records from the very admin editing that company. This was confirmed as a real bug, not a theoretical one, by testing with an admin whose own `Employee` record happened to be linked to a company.

Don't default to plain `models.Manager()` "to be safe" — that's the wrong default and will silently break `CompanyMiddleware`-based scoping for genuinely tenant-owned data. Use `HorillaCompanyManager` unless your model matches the specific staff-cross-company-admin-data pattern above, and document why when you do deviate.

## 4. Choice fields

New app-local models: use Django's `TextChoices` pattern —

```python
class SomeStatus(models.TextChoices):
    ACTIVE = "active", _("Active")
    INACTIVE = "inactive", _("Inactive")

status = models.CharField(max_length=20, choices=SomeStatus.choices)
```

This keeps the DB column a plain `VARCHAR` (no native enum), so adding a new choice later is a one-line Python change with no migration. `base/models.py`'s older models use plain tuples instead (`STATUS_CHOICES = (("active", _("Active")), ...)`) — that predates this convention. Match whichever file you're actually editing; don't convert existing tuples to `TextChoices` as an unrelated drive-by change.

## 5. If you're building a multi-step wizard

A reasonable shape: Draft (relaxed, minimal required fields) → Next (strict, per-step) → Back (relaxed, never blocks) → a final independent re-validation gate that re-derives pass/fail from current DB state every time, rather than trusting an earlier "Next already passed."

**The gotcha to avoid:** if you build a `strict=` toggle mixin for your forms, make sure it actually *enforces* required fields on the strict path, not just *relaxes* them on the loose path. A mixin that only ever does `field.required = False` when not strict — and never sets `field.required = True` — leaves enforcement entirely dependent on the underlying model field's own `blank=` attribute. On a past app this silently meant a whole set of fields was never actually enforced on "Next," discovered only when specifically tested. If you write this kind of mixin, either make it symmetric (also set `required = True` when strict, for fields not in some `always_required`-style opt-out list), or, if you keep it asymmetric, write explicit per-field checks for anything that must be mandatory on the strict tier — and test the strict tier directly, not just infer it from the relaxed one passing.

## 6. Permissions

- Default path: add your app to `base/signals.py`'s `_DEFAULT_HRMS_GROUPS` (whole-app-level `apps`/`actions`, optionally per-app `app_actions` overrides) and `_HRMS_GROUP_MIGRATE_APPS`. This covers the vast majority of cases.
- **When you need a permission slice narrower than "all of one app's models"** (e.g. one group should only touch two specific models inside a large app, or should touch a permission that lives on a model in a *different* app than the one being migrated) — `_DEFAULT_HRMS_GROUPS`' `app_actions` is app-level, not model-level, and structurally cannot express this. Add a small, additive, idempotent `post_migrate` receiver in your own app's `signals.py` instead, filtered to `sender.label == "your_app"`, that grants the specific `Permission` rows directly (`group.permissions.add(*perms)`, never remove). Safe to leave firing on every `migrate` — permissions for a model don't exist until that model's migrations have run, so `post_migrate` (guarded to only act on your own app's label) is the reliable hook, and additive/idempotent writes mean re-running it every time is harmless.

## 7. Encrypting a field that also needs uniqueness

Don't put `unique=True` directly on an `EncryptedCharField`. Fernet ciphertext includes a random IV, so the same plaintext produces different ciphertext every time — a DB-level uniqueness check on the encrypted column will never actually catch a real duplicate. Instead (see `Company.pan`/`Company.pan_hash` in `base/models.py` for the worked example):

1. Store the value itself in an `EncryptedCharField`.
2. Add a companion `<field>_hash = models.CharField(max_length=64, editable=False)`.
3. Compute it with `base.encryption.hash_value()` (a deterministic HMAC — same plaintext always hashes the same way, but isn't reversible) whenever the plaintext changes, typically in `clean()`/`save()`.
4. Put the `UniqueConstraint` on the `_hash` column, not the encrypted one.

## 8. Migrations — verify before applying anything structural

Before applying a migration that renames/moves something already-existing in a real database (an app label, a table, a field's import path), use `python manage.py sqlmigrate <app> <migration>` first and read the actual SQL/no-op output. If you need to fix Django's own bookkeeping (`django_content_type.app_label`, `django_migrations.app`) after a Python-level rename, that has to happen as a **standalone step before `migrate` runs** — not from inside a migration for the renamed app itself, because Django computes its entire migration plan up front from whatever's currently in `django_migrations`, before any operation (including a `RunPython` step) actually executes.

## 9. Repeatable rows in a form (multiple rows, each with several fields)

Prefer a prefix-indexed pattern over a Django formset when rows have multiple correlated fields and you don't want formset management-form bookkeeping: scan submitted POST keys for `{prefix}{index}-{field}` to discover which row indices were submitted, then build one `ModelForm(data, prefix=f"{prefix}{index}")` per discovered index.

## 10. Tests

- Use `horilla.testkit.factories` (`make_company`, `make_user`, `make_employee`, ...) over hand-building model graphs.
- For company-scoped models, use `horilla.testkit.company.CompanyFilterTestMixin` (or `set_selected_company`/`clear_selected_company` directly) to control the tenancy context — never mutate model class attributes directly to fake it.
- Tests live in `<app>/tests/`, not a top-level `tests/` directory.
- When a field/behavior has multiple enforcement tiers (e.g. "required on Next" vs. "required on final activation"), write a test for each tier explicitly — one passing does not imply the other does (§5).
