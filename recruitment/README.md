# recruitment — developer guide

Krew's recruitment module: job openings, stages and the candidate pipeline, the public career page with application forms, contact verification, and the hiring handoff. It started as upstream Horilla's `recruitment` app and was reworked to match the Krew recruitment PRD. This file covers the rules and gotchas that aren't obvious from reading a single file. Read it before you change a model, view or API endpoint here.

## 1. Ground rules for changing this app

- **Upstream code is commented out, never deleted.** Features not in the PRD are disabled by commenting them out: Dashboard, Configuration, Candidate Portal settings, bulk update, and the API endpoints for skills, skill zones, ratings, interview schedules, reject reasons, document requests and LinkedIn. Krew-written code that becomes dead is deleted.
- **UI and API enforce the same rules.** Every rule lives server-side, in `services/` or in a model `save()`/`delete()` guard. Both views and API views call those. Hiding a button is presentation only. If a rule is added for the UI, the API must refuse the same action (§7).
- **Permissions stay inside this module.** Recruitment authority is decided in `services/authorization.py`; don't change other apps' permission code for recruitment needs.

## 2. Layout

| Path | What lives there |
|---|---|
| `services/job_opening.py` | Lifecycle transitions, publication checks, auto-close, career-page slug/origins/caching |
| `services/candidate.py` | Candidate create, stage moves, reject, hire, bulk actions, access checks |
| `services/authorization.py` | Company scope, drive-manager checks, company resolution for new openings |
| `services/screening.py` | Template questions → published snapshot, answer submission |
| `services/verification.py` | Email link + SMS OTP verification for public applications |
| `services/audit.py` | `RecruitmentAuditEvent` writes |
| `views/` | Function views (`career_page.py`, `lifecycle.py`, `surveys.py`, `actions.py`, …) |
| `cbv/` | Generic CBV pages (job opening list, pipeline, stages, candidates, pool, profile) |
| `scheduler.py` | APScheduler job (§6) |
| `templates/` + `horilla_theme/templates/` | Theme templates **shadow** app templates. Check both when an edit doesn't show up |

## 3. Job opening lifecycle

`Recruitment.status` is the source of truth: `DRAFT → REVIEW → PUBLISHED → CLOSED`, with `REMOVED` reachable as a soft delete. `is_active`, `is_published` and `closed` are derived mirrors written only by `Recruitment.apply_status()`. **Never set them directly**; that's why the legacy Archive toggle was removed from the list row.

- All transitions go through `services/job_opening.py` (`submit_for_review`, `send_back_for_changes`, `publish`, `close`, `remove`). Each locks the row, validates the transition, and writes an audit event.
- **Review & Publish** opens the same summary screen (`job-opening-review-summary`) from the list row and the detail page. Publishing is refused while `publication_blockers()` returns anything.
- **Close:** manual Close is available on every published opening. An opening with an End Date also closes automatically (§6). Closing notifies the opening's **managers only**.
- **Closed openings** stay in the list (the Closed filter defaults to All). They allow only **View pipeline, Duplicate and Remove**: no Edit and no Career page. The API refuses updates to closed or removed openings too.
- **Draft/In Review** openings show no pipeline icon; the pipeline lists only live openings (`appears_in_pipeline`).
- **No hard delete.** Openings are removed (soft); the delete endpoint is hidden in the UI and commented out in the API.

## 4. Stages and candidates

- Every opening is seeded with fixed stages: **Applied** at the start, then **Final HR Round (9998)**, **Hired (9999)** and **Rejected (10000)** at the end. Custom stages must sit between Applied and 9998.
- `Stage.save()`/`Stage.delete()` enforce that rule: fixed stages can't be renamed, reordered or deleted; a stage can't move to another opening; only a **drive manager** of that opening may delete a stage. Stage delete is atomic, and removing a stage manager can't leave a stage with none.
- Stage reorder (`update_stage_order` / `stage_sequence_update`) is scoped to the drive and locked for fixed stages.
- Candidate stage moves go through `services/candidate.move_to_stage()`, which checks end states (no moving out of Hired/Rejected) and authority. Bulk move/reject requires the user to be a drive manager for **every** selected candidate.
- **Candidates are never deleted.** Single and bulk delete are refused (`CANDIDATE_DELETE_REFUSED`), and the API PUT/DELETE for candidates is commented out. Rejection is the end state.
- Fields such as stage, status and company are protected on the API (`PROTECTED_CANDIDATE_FIELDS`); they change only through the services.
- Resume uploads are validated by `validate_candidate_document` (real PDF via magic bytes, max 15 MB).

## 5. Templates, questions and the public application

- Question templates (`RecruitmentSurvey` + template groups) are managed in one **template editor**, which also replaced the old Configure Questions screen. The editor has two tabs: **Application form** and **Handoff**.
- At publish time, the opening's questions are frozen into a snapshot (`services/screening.build_snapshot`). Applicants answer the snapshot, not the live templates.
- **Delete rules:** a template, or a question in one, can't be deleted while any opening uses it (`RecruitmentSurvey.openings_using()`). The UI uses its own `Swal.fire` confirm (`krewDeleteTemplate`), because Horilla replaces `window.confirm` with a non-blocking SweetAlert.

### Contact verification (`services/verification.py`, `ApplicationContactVerification`)

| Rule | Value |
|---|---|
| Email link / OTP window | 10 minutes (`VALIDITY`); after it ends, verification restarts |
| Resend | every 30 s (`OTP_RESEND_INTERVAL`); the same code inside the window |
| Wrong attempts | 15 max (`MAX_OTP_ATTEMPTS`) |
| Completed verification stays usable | 24 h (`COMPLETED_VALIDITY`) |

- **Verification doesn't block submission.** If the company has the verification toggle on, the form offers Verify buttons, but an unverified application is still accepted, with its verified status recorded on the candidate.
- If the SMS send fails, the OTP is **not** saved and the user sees an honest error; the code never reports a failed SMS as sent.
- Error messages are distinct: incorrect code, expired, and too many attempts.

## 6. Scheduler (`scheduler.py`)

- Runs **one job**, `recruitment_close` (hourly), which closes openings past their End Date via `close(..., system=True)`. There is no acting user, so `_transition` locks the row through `Recruitment.default`.
- Jobs are stored in the `django_apscheduler` tables (DjangoJobStore), so runs show up in `django_apscheduler_djangojobexecution`. There is no custom scheduler table.
- Started from `RecruitmentConfig.ready()` only in a server process. An `fcntl` lock file (`krew-recruitment-scheduler.lock`) ensures one scheduler per container; production runs one ECS task.
- `RECRUITMENT_SCHEDULER=0` switches it off, `=1` forces it on.
- `candidate_convert`, the interview sync and document retention were **removed on purpose**. Retention is a later phase.

## 7. Career page (public)

| URL | Purpose |
|---|---|
| `/recruitment/careers/<slug>/` | A company's public list of published openings (embeddable in an iframe) |
| `/recruitment/careers/<slug>/jobs/<id>/` | Public View page for one opening |
| `/recruitment/careers/<slug>/apply/<id>/` | Apply; a closed opening redirects back to the list |

- **Slug only, no company id.** `career_page_slug(company)` is a plain `slugify` of the company name, with `-2`, `-3`… added on a clash. The View and Apply routes check that the opening belongs to the slug's company, so changing the id in the URL doesn't reach another company's openings.
- **Framing allowlist:** `RecruitmentGeneralSetting.career_page_domains` lists the origins allowed to iframe the page. `apply_career_page_framing()` turns that into a per-company CSP `frame-ancestors` header.
- **Career Page popup** (`job-opening-career-page`) on each live opening gives the steps, the iframe code and the links, with copy buttons.
- `recruitment_details/<id>/` remains upstream's `@login_required` page; visitors use the slug routes.

### Caching (Redis when `REDIS_URL` is set, otherwise LocMem)

| Key | Contents | Invalidation |
|---|---|---|
| `recruitment:public-listing:<version>:<company>` | Rendered public listing (TTL 300 s) | `post_save` on `Recruitment` bumps the version (`invalidate_public_listing`) |
| `recruitment:career-origins:<company_id>` | Allowed frame origins | `invalidate_career_origins` when the setting changes |
| `recruitment:career-slug:<slug>` | slug → company id | — |
| pipeline filter (per session) | Last pipeline filter (TTL 600 s) | — |

After changing a career-page template, call `invalidate_public_listing()`, or wait out the TTL. Otherwise visitors keep seeing the cached HTML.

## 8. REST API (`horilla_api`, prefix `/api/recruitment/`)

- Every view extends `RecruitmentAPIView`. JWT auth runs inside the view, after `CompanyMiddleware`, so `initial()` re-runs the middleware to set the company context. **Don't add a recruitment API view that skips this base**, or its querysets will be unfiltered.
- **Active endpoints:**
  - `recruitment/`, `recruitment/<pk>/`, plus lifecycle `recruitment/<pk>/<action>/` with `action` in `submit-for-review | send-back | publish | close | remove`. This route must stay **last** in the URL list.
  - `stage/`, `stage/<pk>/`, `recruitment/<id>/stage/`
  - `candidate/`, `candidate/<pk>/`, `recruitment/<id>/candidate/`, `stage/<id>/candidate/`
  - `survey-template/`, `survey-template/<pk>/`
  - `rejected-candidate/…`, `candidate-document/…`
  - `job-opening/<pk>/questions/`, `candidate/<pk>/screening-answers/`
- **Create rules:**
  - Opening and candidate creates go through the same services as the UI (company resolution, `record_created`, duplicate-email check).
  - `serializer.validators = []` is set on purpose: DRF's unique-together validators would otherwise force fields the service fills in.
- **Refused writes (match the UI):**
  - opening DELETE; edits to closed or removed openings;
  - candidate PUT and DELETE;
  - rejected-candidate writes;
  - document PUT and DELETE (POST goes through `upload_document`);
  - template DELETE while the template is in use.

## 9. Migrations

`0007`–`0028` are Krew's. Several are **data backfills**: `0008` status, `0010` company stamp, `0013` screening config, `0018` terminal stages, `0019` retiring the default initial stage. Don't squash or edit them; add new migrations instead. Check with `python manage.py makemigrations --check` before calling a model change done.

## 10. Settings / environment

Recruitment-related values are in `.env.example`:

- **Email (SES):** `SES_API_URL`, `SES_API_KEY`
- **SMS OTP:** `SMS_PROVIDER`, `SMS_API_KEY`, `SMS_DEFAULT_COUNTRY_CODE`
- **Google OAuth:** `OAUTHLIB_RELAX_TOKEN_SCOPE`

Never commit real keys.

## 11. Tests

Only the upstream suites remain in `tests/` (`test_candidate_stage.py`, `test_manager_helpers.py`). New suites should use `horilla.testkit` (`make_company`, `make_employee`, `CompanyFilterTestMixin`). When a test touches files, limit it to a probe object: rolling back the database does not restore deleted media files.
