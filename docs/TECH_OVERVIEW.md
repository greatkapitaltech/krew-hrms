# krew-hrms Technical Overview

This is a working-code walkthrough of `krew-hrms`, a customized fork of
**Horilla HRMS** (Django). It's meant to get a new contributor from "where do
I even start" to "I can trace a request end-to-end" using two real,
concrete flows from this codebase rather than abstract description.

For local setup, see [`SETUP.md`](../SETUP.md). For deploy/infra, see
[`docs/deploy.md`](deploy.md) and [`docs/redis-and-rls.md`](redis-and-rls.md).

## 1. Stack

- **Django 5.2**, server-rendered templates + htmx-style partial reloads (not an SPA)
- **PostgreSQL** as the primary database
- **Redis** for cache / rate-limiting (see `docs/redis-and-rls.md`)
- **Django REST Framework + SimpleJWT** for the API surface (`horilla_api/`)
- **APScheduler** (`django_apscheduler`) for in-process cron-style jobs
- Multi-tenant: one Django deployment serves **multiple companies**, each
  employee's data scoped by `Company`

## 2. How the codebase is organized

Each top-level directory is a **Django app**, one per HR domain:
`employee`, `attendance`, `leave`, `payroll`, `recruitment`, `onboarding`,
`offboarding`, `asset`, `pms` (performance), `helpdesk`, `project`,
`biometric`, `whatsapp`, `report`, etc.

Framework/plumbing apps (prefixed `horilla_*`) are shared infrastructure used
by every domain app:

| App | Purpose |
|---|---|
| `base` | Core models (`Company`, `Employee`-adjacent config), auth glue, middleware, decorators — the foundation everything else imports |
| `horilla_auth` | Custom user model (`HorillaUser`, swapped in as `AUTH_USER_MODEL`) |
| `horilla_views` | The declarative **generic CBV framework** (list/detail/form/kanban views) used across almost every app — see §5 |
| `horilla_api` | DRF app exposing a REST/JWT API mirroring the web UI's data |
| `horilla_widgets`, `horilla_crumbs`, `horilla_theme` | UI chrome (breadcrumbs, reusable widgets, theming) |
| `horilla_automations` | User-configurable "if X then Y" automation rules |
| `horilla_audit` | Change history / audit trail (`django-simple-history` + `django-auditlog` wrapper) |
| `horilla_backup`, `pg_backup` | Scheduled DB/media backups |
| `notifications` | In-app notification inbox (`notify.send(...)`, used everywhere) |
| `krew_company_onboarding` | This fork's custom company-onboarding + bank-verification wizard (Cashfree integration) — not upstream Horilla |

Within a domain app, the recurring file layout is:

```
<app>/
  models.py        # DB schema (HorillaModel subclasses)
  views.py          # legacy function-based views (FBVs)
  cbv/               # newer class-based views (HorillaListView/FormView/...)
  forms.py, filters.py
  urls.py
  signals.py         # post_save/pre_save side effects
  scheduler.py        # APScheduler jobs registered on app startup
  sidebar.py           # left-nav menu entries
  templates/, static/
```

Note the coexistence of `views.py` (older function-based views) and `cbv/`
(newer declarative class-based views) — both patterns are live in this
codebase; §5 and §6 below cover one example of each.

## 3. Request lifecycle & multi-tenancy

Every request passes through `horilla/settings/base.py`'s `MIDDLEWARE` list
(`horilla/settings/base.py:140`), in order. Two of these are load-bearing for
understanding *any* view in this app:

```
django.contrib.auth.middleware.AuthenticationMiddleware   # sets request.user
base.middleware.CompanyMiddleware                          # picks the "current company"
base.middleware.ForcePasswordChangeMiddleware
base.middleware.TwoFactorAuthMiddleware
...
```

**`CompanyMiddleware`** (`base/middleware.py:207`) is the crux of multi-tenancy.
On every authenticated request it:

1. Reads `request.session["selected_company"]` (or falls back to the
   logged-in employee's own `Company` on first login).
2. Clamps that choice to companies the user is actually allowed to see
   (`_clamp_to_allowed`, only relevant when `COMPANY_SCOPED_PERMISSIONS` is on).
3. Stores the resolved company id in a **`contextvar`** via
   `set_selected_company(company_id)` (`horilla/horilla_middlewares.py:113`) —
   *not* on the request object. A contextvar is visible from anywhere in the
   call stack for that request/thread, without having to thread `request`
   through every function.

That contextvar is then read by **`HorillaCompanyManager`**
(`base/horilla_company_manager.py:126`), the custom Django model `Manager`
that almost every model in this project uses instead of `models.Manager()`.
Its `get_queryset()` override does:

```python
company = get_selected_company()          # reads the contextvar set above
filter_path = self.get_company_filter_path()  # e.g. "employee_id__employee_work_info__company_id"
return qs.filter(Q(**{filter_path: company}) | Q(**{f"{filter_path}__isnull": True}))
```

**This is the single mechanism that makes the app multi-tenant.** A view
never has to remember to add `.filter(company=...)` — as long as a model
declares `objects = HorillaCompanyManager(related_company_field="...")`,
every `Model.objects.all()` / `.filter()` call anywhere in the codebase is
already scoped to whatever company the middleware resolved for this request.
`HorillaCompanyManager.all()` additionally hides soft-deleted/inactive rows
by default (respecting an `?is_active=` query param override) — see
`base/horilla_company_manager.py:181`.

Concretely, `AvailableLeave` (`leave/models.py:652`) declares:

```python
objects = HorillaCompanyManager(
    related_company_field="employee_id__employee_work_info__company_id"
)
```

so `AvailableLeave.objects.filter(employee_id=emp)` transparently becomes
"available leave for this employee, in whichever company the request has
selected."

## 4. Example flow #1 — Login

Trace of what happens when someone submits `templates/login.html`
(the file currently open in the IDE renders through this same view — note
there are two `login.html` templates: `templates/login.html`, which is what
`login_user` actually renders, and the standalone `auth/login.html`).

1. **URL**: `base/urls.py:319` → `path("login/", views.login_user, name="login")`
2. **View**: `login_user` (`base/views.py:769`), a plain function-based view:
   - `GET` → renders `login.html`.
   - `POST` → `authenticate(request, username=..., password=...)`. This
     runs Django's configured `AUTHENTICATION_BACKENDS` in order (LDAP,
     Microsoft/Outlook OAuth, company-scoped permission backend, then the
     default) — see `base/auth_backends.py` / `horilla_ldap/` /
     `outlook_auth/backends.py`.
   - On success it checks two extra invariants Django's default backend
     doesn't: the `HorillaUser` must have a linked `employee_get`
     (`Employee`), and that employee must be `is_active` — otherwise it
     bounces back to `login` with a message rather than letting a
     user-without-an-employee-record into the app.
   - `login(request, user)` — standard Django session login.
   - Redirects to `?next=` if it's a safe same-host URL
     (`url_has_allowed_host_and_scheme` — this guards against open-redirect
     attacks via a crafted `next` param), otherwise `/`.
3. **Next request** (e.g. the dashboard redirect): `AuthenticationMiddleware`
   now populates `request.user` from the session, then `CompanyMiddleware`
   (§3) resolves and stores that user's default company, and every
   `HorillaCompanyManager`-backed query from here on is scoped to it.
4. **Logout** (`logout_user`, `base/views.py:1096`) does `logout(request)`
   and returns a tiny HTML snippet that clears `localStorage` client-side
   before redirecting to `/login/` — cleaning up any client-cached state
   (e.g. theme, table column prefs) that shouldn't survive a session switch.

## 5. Example flow #2a — the declarative list-view framework (`horilla_views`)

Newer screens (most of `leave/cbv/`, `attendance/cbv/`, `payroll/cbv/`, …)
don't write template-rendering logic by hand. They subclass generic CBVs
from `horilla_views/generic/cbv/views.py` and declare *what* to show; the
base class handles pagination, search, column visibility toggles, and
row-action rendering.

`leave/cbv/my_leave_request.py:60` — the "My Leave Requests" table an
employee sees on their own dashboard:

```python
@method_decorator(login_required, name="dispatch")
class MainParentListView(HorillaListView):
    filter_class = UserLeaveRequestFilter
    model = LeaveRequest
    columns = [
        (_("Leave Type"), "leave_type_custom"),
        (_("Start Date"), "start_date"),
        (_("End Date"), "end_date"),
        (_("Requested Days"), "requested_days"),
        (_("Status"), "custom_status_col"),
        (_("Comment"), "comment_action"),
    ]
    action_method = "leave_actions"
```

What happens on `GET`:
1. `HorillaListView.get_queryset()` (in `horilla_views/generic/cbv/views.py`)
   starts from `LeaveRequest.objects.all()` — already company-scoped per §3
   — then applies `filter_class` (a `django_filter.FilterSet`) against the
   query string for search/filter widgets.
2. For each declared column, if the string matches a model field it's
   rendered directly; if not (e.g. `"leave_type_custom"`, `"custom_status_col"`)
   the base view looks for a same-named method on the *model instance* and
   calls it — this is how `LeaveRequest` supplies custom-rendered cells
   (colored status badges, etc.) without the view needing per-column
   branching logic.
3. `action_method` names a method that returns the row's action buttons
   (edit/delete/approve), so permission checks for those buttons live next
   to the model/view, not duplicated in a template.
4. The rendered HTML is a partial (loaded via the page's own AJAX/htmx call),
   so paging/sorting/filtering re-hits this same view and swaps only the
   table body — no full page reload.

The payoff: adding a new list screen elsewhere in the app is "declare a
model + columns + filter class," not "write a queryset, a paginator, and a
template loop" from scratch every time.

## 6. Example flow #2b — creating a Leave Request (FBV + signals)

This is the older, more explicit style, and it's worth tracing in full
because it shows how several independent subsystems (leave balances,
attendance, notifications) stay in sync **without calling each other
directly** — via Django signals.

**a) The view** — `leave_request_create` (`leave/views.py:3303`):

```python
form = UserLeaveRequestCreationForm(request.POST, request.FILES, employee=emp)
if form.is_valid():
    leave_request = form.save(commit=False)
    if leave_request.leave_type_id.require_approval == "no":
        # auto-approve path: deduct from AvailableLeave immediately,
        # dipping into carryforward_days if the request exceeds the balance
        ...
        leave_request.status = "approved"
        available_leave.save()
    leave_request.created_by = request.user.employee_get
    leave_request.save()
    ...
    notify.send(request.user.employee_get, recipient=manager, verb="...", redirect=f"/leave/request-view?id={leave_request.id}")
```

Key details:
- It only auto-approves and deducts `AvailableLeave` balance *in the view*
  when the `LeaveType` is configured not to require approval. Otherwise the
  request is saved with its default `status` ("requested") and balance
  deduction happens later, at approval time (elsewhere in `leave/views.py`).
- `multiple_approvals_check()` looks up whether this leave type has a
  configured multi-level approval chain and, if so, notifies the first
  manager in that chain instead of the direct reporting manager.
- `notify.send(...)` (from `notifications`) creates an in-app notification
  row *and* is wrapped in `contextlib.suppress(Exception)` — a notification
  failure (e.g. the manager has no linked user) must never block the leave
  request itself from saving.

**b) The signal** — `leaverequest_pre_save` in `leave/signals.py:17`, connected
via `@receiver(post_save, sender=LeaveRequest)`, fires automatically every
time *any* code path saves a `LeaveRequest` — not just this view. When
`status == "approved"`, it walks every date in the leave's range
(`instance.requested_dates()`) and upserts an `attendance.WorkRecords` row
marking that day as a leave day (with `day_percentage` set to 0.5 for a
half-day breakdown). This is why the Attendance module "just knows" about
approved leave without `leave/views.py` importing anything from
`attendance` directly — the coupling is a signal, not a function call,
which is deliberate: `leave` doesn't need to know `attendance` exists, and
`attendance` app can be disabled (the signal is gated behind
`if apps.is_installed("attendance")`) without breaking leave requests.

**Full picture:**

```
POST /leave/leave-request-create/
  └─ leave_request_create (leave/views.py)
       ├─ validates UserLeaveRequestCreationForm
       ├─ (if no-approval-required) deducts AvailableLeave now, status="approved"
       ├─ leave_request.save()
       │     └─ post_save signal → leaverequest_pre_save (leave/signals.py)
       │           └─ if approved: upsert attendance.WorkRecords for each date
       └─ notify.send(...) → manager's notification inbox (notifications app)
```

## 7. Permissions

Three layers coexist, from broadest to narrowest:

- **Django's built-in permission system** — `request.user.has_perm("leave.change_leaverequest")`,
  backed by `django.contrib.auth`'s `Permission`/`Group` model, checked directly in views/templates.
- **Company-scoped permissions** — when `COMPANY_SCOPED_PERMISSIONS` is enabled,
  `CompanyGroupAssignment` (`base/models.py:301`) makes a user's group membership,
  and therefore their permissions, apply *only within specific companies* — resolved by
  `base.auth_backends.CompanyScopedBackend` rather than Django's default backend.
- **Custom decorators for relationship-based access** — e.g.
  `manager_can_enter` (`base/decorators.py:77`) grants access not just to users
  with the raw Django permission, but also to anyone who is someone's
  *reporting manager* (`EmployeeWorkInformation.objects.filter(reporting_manager_id=employee)`)
  or, for leave specifically, an approver named in `MultipleApprovalManagers`.
  This is why an HR manager can approve a report's leave request without
  ever being granted `leave.change_leaverequest` as a raw Django permission.

## 8. Background jobs

Each domain app that needs periodic work defines a `scheduler.py`
(`leave/scheduler.py`, `attendance/scheduler.py`, `payroll/scheduler.py`,
`horilla_backup/scheduler.py`, `pg_backup/scheduler.py`, …) registered with
`django_apscheduler` on app startup — e.g. nightly leave-balance resets,
scheduled DB backups, biometric device syncs. These run in-process
alongside the web server rather than as a separate worker/queue, so they're
one of the first places to look when investigating "why did X change
overnight with no user action."

## 9. API layer

`horilla_api/` exposes a parallel REST surface (DRF, JWT auth via
`rest_framework_simplejwt`) documented through `drf-yasg` (Swagger UI). It's
organized the same way as the web app but split into
`api_serializers/`, `api_views/`, `api_filters/`, `api_urls/` per domain —
so the leave-request API sits alongside `leave/`'s web views conceptually,
just in its own directory tree. Because it reuses the same models (and
therefore the same `HorillaCompanyManager`), API requests are
company-scoped the same way web requests are, as long as the authenticated
API user resolves to an `Employee` the same way `login_user` requires above.

## 10. Where to go next

- Pick a domain app you care about (e.g. `attendance/`) and read `models.py`
  → `signals.py` → `views.py`/`cbv/` in that order; that's the same trace
  order used in §6.
- `horilla_views/generic/cbv/views.py` is worth reading in full once — it's
  the base class behind most list/detail/form screens, so understanding it
  once pays off across every app.
- `base/horilla_company_manager.py` and `base/middleware.py` are the two
  files to reread whenever multi-tenant behavior looks wrong (data leaking
  across companies, or a company's own data going missing).
