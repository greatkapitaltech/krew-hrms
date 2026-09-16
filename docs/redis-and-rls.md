# Design: Redis and Row-Level Security

Status: **proposed** · Applies to: krew-hrms (Django 5.2 / PostgreSQL 16)

Two independent pieces of work, written up together because both were scoped in
the same session. Redis is small and self-contained. RLS is a multi-phase change
to how tenant isolation is enforced, and starts in **logging-only** mode.

---

## Part 1 — Redis

### Role

Redis is the **Django cache backend, and nothing else.**

It is explicitly *not*: the session store, a Celery/RQ broker, a Channels layer,
or a lock service. Those may come later, each as its own decision — but every one
of them turns Redis from a disposable cache into a component whose loss is an
outage. Keeping it cache-only means **Redis can be wiped or lost at any moment
with no data loss and no user impact.** That property is worth protecting, and
it is the reason this document says no to the other uses for now.

### Current state

`horilla/settings/base.py:206` already does the right thing:

```python
if REDIS_URL:
    CACHES = {"default": {"BACKEND": "django_redis.cache.RedisCache", ...}}
```

No `REDIS_URL` → Django's in-memory LocMem cache. `django-redis==5.4.0` is
already in `requirements.txt`. The all-in-Docker stack (`docker-compose.yml`)
runs Redis; the local dev stack (`docker-compose.dev.yml`) does not.

So the only gap is **local dev parity**.

### Changes

1. Add a `redis:7-alpine` service to `docker-compose.dev.yml`, alongside
   `db` and `pgadmin`.
2. Add `REDIS_URL=redis://localhost:6379/0` to `.env.example`, commented, with a
   note that leaving it unset is a valid choice (LocMem).
3. Set `maxmemory-policy allkeys-lru` — correct for a pure cache, and wrong the
   moment anything non-disposable is stored. Revisit if that ever changes.
4. Set `IGNORE_EXCEPTIONS: True` plus `django_redis.exceptions.ConnectionInterrupted`
   logging in the `CACHES` OPTIONS.

Point 4 matters more than it looks. Without it, **a Redis outage turns every
cached page into a 500** — the cache becomes a hard dependency by accident. With
it, a Redis outage degrades to "slower", which is what a cache should do.

### Keyspace

`KEY_PREFIX` is currently `"horilla"`. Change to `"krew"` for consistency with
the rebrand. This invalidates the existing cache on deploy, which is harmless by
definition — if it is not harmless, something is being stored in Redis that
should not be.

### Not doing, and why

| Use | Why not now |
| --- | --- |
| Sessions | Would make Redis loss a mass-logout event. DB sessions are fine at current scale. |
| Scheduler locks | Real problem (`django-apscheduler` double-fires across workers) but only above one worker. Worth doing when you scale out — not before. |
| Task queue | Adds a worker process, retries and monitoring. A genuine architectural change, not a config change. Separate decision. |

---

## Part 2 — Row-Level Security

### Goal

Move tenant isolation from "every queryset remembers to filter" to "the database
refuses to return the row". Today a single missing `.filter(company_id=...)` in
any of ~60 tenant-scoped models leaks across companies, and nothing catches it.

### What RLS can and cannot do here

This distinction drives the whole design.

Authorization in this codebase is **Django permissions** — `request.user.has_perm(...)`,
group membership, `is_superuser`, plus manager checks (`horilla/decorators.py:45,59,139`).
That logic lives in Python and is not reproducible in a SQL policy without
reimplementing Django's permission resolution in the database, which would rot
out of sync immediately.

Therefore:

- **The application stays the authority on _role_** — "is this user allowed to
  act as HR?" is answered in Python, as it is today.
- **The database becomes the authority on _scope_** — "given that role, which
  rows may this session see?" is answered by policy.

RLS here is **defence in depth against a forgotten filter.** It is not a
replacement for the permission system, and it must not be sold as one.

### Context propagation

The app already tracks the current company in a ContextVar
(`horilla/horilla_middlewares.py:19`). New middleware pushes that context into
the database session as GUCs:

```sql
SET LOCAL app.current_company  = '42';
SET LOCAL app.current_employee = '1337';
SET LOCAL app.can_read_all     = 'on';   -- computed by Django from has_perm()
```

Policies then read them via `current_setting('app.current_company', true)`.

#### The connection-reuse hazard

`SET LOCAL` is scoped to a transaction. Outside a transaction it silently
degrades to a session-level `SET`, which **persists on the connection** — and a
pooled connection handed to the next user carries the previous user's company.
That is the exact cross-tenant leak this work exists to prevent, reintroduced by
the mitigation.

Today the app is safe by accident: no `CONN_MAX_AGE` and no `ATOMIC_REQUESTS`
means autocommit with a fresh connection per request. That safety disappears the
moment anyone sets `CONN_MAX_AGE`, or puts PgBouncer in front in transaction
pooling mode.

So the design must not rely on it:

1. Wrap the request in an explicit transaction (`ATOMIC_REQUESTS = True`, or an
   explicit `atomic()` in the middleware) so `SET LOCAL` is always genuinely
   transaction-local.
2. **Reset the GUCs on connection checkout as well** — belt and braces, so a
   leaked session variable cannot outlive its request.
3. A test that asserts the GUC is empty at the start of a request.

Note that `ATOMIC_REQUESTS = True` is itself a behaviour change: every request
becomes one transaction, so a view that currently commits partial work will
instead roll it all back on error. That needs its own review before flipping.

### Database roles

**This is the part that makes RLS actually take effect**, and the reason the
current setup would silently no-op.

The app connects as `horilla_user`, which the Postgres image created as a
**superuser** and which **owns every table**. Superusers bypass RLS entirely;
owners bypass it unless the table is `FORCE ROW LEVEL SECURITY`. Policies would
be installed, look correct, and do nothing.

New role split:

| Role | Superuser | Owns tables | Used by |
| --- | --- | --- | --- |
| `krew_owner` | no | yes | migrations, `manage.py` admin tasks |
| `krew_app` | no | no | the running application |

`krew_app` gets `SELECT/INSERT/UPDATE/DELETE` on the tables and is subject to
policy. Neither role has `BYPASSRLS`. `FORCE ROW LEVEL SECURITY` is set on
policied tables so that even the owner is constrained.

This changes `docker-compose.dev.yml`, `.env.example` and `SETUP.md`, and needs a
migration path for existing databases.

### Policy shape

Two predicates, combined per table:

```sql
-- tenant isolation (all ~60 company-scoped tables)
company_id = current_setting('app.current_company')::int

-- row visibility (payroll, attendance, leave)
AND (
      current_setting('app.can_read_all') = 'on'          -- HR/admin, decided by Django
   OR employee_id = current_setting('app.current_employee')::int   -- own records
   OR employee_id IN (SELECT ...subordinates...)          -- manager
)
```

#### The subordinate lookup is the performance risk

"Subordinates" is not a column. It is `employee_work_info.reporting_manager_id`
(`horilla/decorators.py:36`) — one join away from `Employee`. A correlated
subquery in a policy is evaluated **per row**, on every read of payroll,
attendance and leave.

Mitigations, in order of preference:

1. A `STABLE` SQL helper `app_visible_employees()` so the planner caches it per
   statement rather than per row.
2. Denormalise `company_id` onto the hot child tables that lack it, so tenant
   isolation never needs a join.
3. Indexes on every `company_id` used in a policy.

Benchmark before and after on the dashboard queries; this is the single most
likely reason to abandon or narrow the approach.

### Rollout — phase 1 is logging-only

**Phase 1 — observe (no behaviour change).**
Create `krew_app`, install policies, `ENABLE ROW LEVEL SECURITY` *without*
`FORCE`. The app keeps connecting as the owner, so **policies are inert and
nothing changes for users.**

A verification harness then connects *as `krew_app`* (where policy is live) and,
for a representative query set, compares row counts against what the application
returns. Every divergence is logged:

- rows the app returns but RLS would hide → the policy is too strict, or the app
  is leaking. Both are findings.
- rows RLS would allow but the app filters out → policy is too loose.

This yields the real map of where enforcement would break, at zero user risk.
Phase 1 exits when the divergence log is quiet under normal use.

**Phase 2 — enforce.** Point the app at `krew_app`, add `FORCE ROW LEVEL
SECURITY`, table group by table group, most isolated first. Each group ships
with cross-company leak tests.

**Rollback** at any point: `ALTER TABLE ... NO FORCE ROW LEVEL SECURITY`, or
repoint the app at the owner role. Both are instant and need no data migration.

### Testing prerequisite

`unit-tests.yml` runs on **SQLite**, which has no RLS — the entire feature is
untestable in CI today. A Postgres-backed job is a hard prerequisite, not a
follow-up. This is now in place: `.github/workflows/unit-tests.yml` runs the smoke
suite twice, once on SQLite (with the existing coverage floor) and once on
PostgreSQL 16.

### Risks

| Risk | Severity | Handling |
| --- | --- | --- |
| Policies silently inert (superuser/owner) | **critical** — looks fine, protects nothing | dedicated role, `FORCE`, plus a test asserting a leak *is* blocked |
| GUC leaking across pooled connections | **critical** — cross-tenant leak | `SET LOCAL` in an explicit transaction + reset on checkout + test |
| Subordinate subquery cost | high | `STABLE` helper, denormalisation, benchmarks |
| `ATOMIC_REQUESTS` changes commit semantics | medium | review views that rely on partial commits before flipping |
| Background jobs have no request context | medium | APScheduler/biometric jobs need an explicit company context or an exempt role |

That last row is easy to forget: **scheduled jobs and management commands run
with no request**, so they have no GUCs set. Under enforcement they would see
zero rows. They need either an explicit context or a documented exemption.
