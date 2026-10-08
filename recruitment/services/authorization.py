"""
recruitment/services/authorization.py

Object-scoped authorization for Recruitment job openings.

Why this exists rather than reusing recruitment/decorators.py: those
decorators widen a permission with

    Recruitment.objects.filter(recruitment_managers=employee).exists()

which answers "does this employee manage *any* job opening?", not "does this
employee manage *this* one?". A manager of one drive therefore passes the
gate for every drive in their company. The lifecycle services must not be
built on that, so authorization here is always evaluated against a specific
job opening instance.

The existing decorators are left untouched -- other Recruitment views rely on
their current behaviour, and correcting them repo-wide is a separate task.
"""

import logging

from base.auth_backends import get_allowed_company_ids
from horilla.horilla_middlewares import get_selected_company

logger = logging.getLogger(__name__)


def _employee_of(user):
    """The Employee record linked to ``user``, or None."""
    return getattr(user, "employee_get", None)


def company_in_scope(user, company_id):
    """
    True when ``company_id`` is inside the user's authorized company scope.

    A null company is treated as in-scope, matching HorillaCompanyManager,
    which passes ``company_id__isnull=True`` rows through to every tenant --
    legacy/unscoped rows must stay reachable or they become invisible and
    un-editable.
    """
    if user.is_superuser:
        return True
    if company_id is None:
        return True
    try:
        return int(company_id) in get_allowed_company_ids(user)
    except (TypeError, ValueError):
        return False


def manages_job_opening(user, job_opening):
    """True when the user is an assigned manager of *this* job opening."""
    employee = _employee_of(user)
    if employee is None:
        return False
    return job_opening.recruitment_managers.filter(pk=employee.pk).exists()


#: Holding this permission marks company-wide job-opening authority: the
#: ability to create an opening implies authority over the openings in your
#: own company (VWS HR / Client HR). A drive manager deliberately does not
#: hold it, so their authority is limited to the openings they are assigned
#: to. Chosen over inventing a publish_recruitment permission, which the PRD
#: does not call for.
COMPANY_WIDE_AUTHORITY_PERMISSION = "recruitment.add_recruitment"


def has_company_wide_job_opening_authority(user):
    """
    True when the user's authority covers every job opening in their company.

    Replaces an earlier check that asked "does this employee manage *no* job
    opening?" and treated that as company-wide authority. That was wrong twice
    over: a manager assigned to nothing got blanket access, and authority
    shrank as someone was assigned more drives. Authority is a permission
    question, so it is answered from permissions.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    return user.has_perm(COMPANY_WIDE_AUTHORITY_PERMISSION)


def user_can_manage_job_opening(user, job_opening, permission):
    """
    Authorization gate for every job-opening lifecycle operation.

    Enforced in order, all server-side:

    1. the user is authenticated;
    2. the job opening's company is inside the user's company scope;
    3. the user holds ``permission`` (a hard requirement -- being a manager
       is never a substitute for it);
    4. object scope: either company-wide authority (``add_recruitment``), or
       being an assigned manager of *this* specific opening.

    Superusers bypass 3 and 4, matching Django's own behaviour.

    Never returns True merely because the user manages some *other* job
    opening, and never across companies.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if job_opening is None:
        return False

    if not company_in_scope(user, job_opening.company_id_id):
        return False

    if user.is_superuser:
        return True

    if not user.has_perm(permission):
        return False

    # Company-wide authority (holds add_recruitment) covers every opening in
    # the company scope already validated above.
    if has_company_wide_job_opening_authority(user):
        return True

    # Otherwise the only way in is being an assigned manager of THIS opening.
    # Managing some other opening grants nothing here.
    return manages_job_opening(user, job_opening)


def selectable_companies_for_user(user):
    """
    Companies this user may create a job opening for.

    VWS staff operate across client companies by design -- one admin onboards
    and hires for many clients -- so "the creator's company" is not a single
    value for them. This returns the set their login authorizes:

    * superuser -> every company (they hold no CompanyGroupAssignment rows, so
      the assignment-derived set would otherwise be empty);
    * everyone else -> their assignment companies plus their work-info company.

    Used both to offer a choice when there is more than one, and to re-verify
    a submitted choice server-side. A client-supplied company is never trusted.
    """
    from base.models import Company

    if getattr(user, "is_superuser", False):
        return Company.objects.all()
    return Company.objects.filter(id__in=get_allowed_company_ids(user))


def resolve_company_for_new_job_opening(user):
    """
    The company a new job opening belongs to, derived from the login context.

    Company is never a form field (the PRD says it is not shown), so it is
    resolved here, in priority order:

    1. the company selected in the switcher for this session;
    2. the user's own work-info company;
    3. their single authorized company, when they only have one.

    Returns None when genuinely ambiguous -- most often a superuser sitting on
    "All companies", where base.auth_backends.resolve_company_id_for_new_record
    also declines to guess. Callers must refuse the operation in that case
    rather than save a null-company row: HorillaCompanyManager passes
    ``company_id__isnull=True`` through to every tenant, so an unscoped job
    opening is visible company-wide.
    """
    from base.models import Company
    from employee.models import EmployeeWorkInformation

    selected = get_selected_company()
    if selected and selected != "all":
        company = Company.find(selected)
        if company is not None:
            return company

    # Read work info from the database rather than through
    # user.employee_get.employee_work_info: that reverse OneToOne can hold a
    # cache populated before the work-info row was written, which reports
    # company as None even though the row has one.
    employee = getattr(user, "employee_get", None)
    if employee is not None:
        work_company_id = (
            EmployeeWorkInformation.objects.filter(employee_id=employee)
            .values_list("company_id", flat=True)
            .first()
        )
        if work_company_id:
            company = Company.find(work_company_id)
            if company is not None:
                return company

    allowed = get_allowed_company_ids(user)
    if len(allowed) == 1:
        return Company.find(next(iter(allowed)))

    return None


def get_job_opening_for_user(user, pk, permission):
    """
    Fetch a job opening by pk, enforcing company scope and authorization.

    Raises JobOpeningNotFound when the object is missing *or* out of the
    user's company scope, and RecruitmentPermissionDenied when it is visible
    but the user may not act on it. Use this instead of a bare
    ``get_object_or_404`` so a guessed/enumerated id cannot reach a
    lifecycle operation.
    """
    from recruitment.models import Recruitment
    from recruitment.services.errors import (
        JobOpeningNotFound,
        RecruitmentPermissionDenied,
    )

    # `default` (plain manager) so the lookup itself is not silently filtered
    # by the request's selected company -- scope is enforced explicitly below
    # and must produce "not found", not an empty queryset by accident.
    job_opening = Recruitment.default.filter(pk=pk).first()
    if job_opening is None or not company_in_scope(user, job_opening.company_id_id):
        raise JobOpeningNotFound()
    if not user_can_manage_job_opening(user, job_opening, permission):
        raise RecruitmentPermissionDenied()
    return job_opening


def is_drive_manager(user, job_opening):
    """
    PRD drive-level authority: the opening's assigned Managers, company-wide
    HR (VWS HR / Client HR) or a superuser -- never a Stage Manager on that
    basis alone. Used for adding, editing, removing and staffing stages.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if user.is_superuser:
        return True
    # Never across companies: company-wide HR authority is a permission, so it
    # must be limited to the user's own companies here -- an API call (JWT)
    # has no company context to filter lookups by.
    if job_opening is None or not company_in_scope(user, job_opening.company_id_id):
        return False
    if has_company_wide_job_opening_authority(user):
        return True
    return manages_job_opening(user, job_opening)

