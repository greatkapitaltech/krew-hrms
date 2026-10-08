"""
Candidate Pool: the permanent, company-scoped list of everyone who has
interacted with Recruitment.

A Recruitment sub-module in its own right, at the same level as Job Openings.
Distinct from the existing Candidates screen in three ways that matter:

  * it is one row per APPLICATION, and never deduplicates a person -- someone
    who applied to three openings legitimately appears three times;
  * a candidate with no job opening at all is a first-class row;
  * it reports five general status buckets rather than each client's custom
    pipeline stage names.

Everything authorization-sensitive is delegated to
recruitment.services.candidate. In particular get_queryset() goes through
accessible_candidates(), NOT the model manager: the manager passes
company_id__isnull=True rows through to every tenant, so an unscoped candidate
would otherwise surface in every company's pool.

Candidate Detail is deliberately NOT reimplemented here -- rows link to the
same CandidateProfileView the Pipeline uses.
"""

from typing import Any

from django.urls import reverse
from django.utils.decorators import method_decorator
from django.utils.translation import gettext_lazy as _

from horilla_views.cbv_methods import login_required, permission_required
from horilla_views.generic.cbv.views import (
    HorillaFormView,
    HorillaListView,
    HorillaNavView,
    TemplateView,
)
from recruitment.filters import CandidatePoolFilter
from recruitment.forms import PoolCandidateForm
from recruitment.models import Candidate
from recruitment.services import candidate as candidate_service


@method_decorator(login_required, name="dispatch")
@method_decorator(
    permission_required(perm="recruitment.view_candidate"), name="dispatch"
)
class CandidatePoolView(TemplateView):
    """Container page. The nav and table load into it over HTMX."""

    template_name = "cbv/candidate_pool/candidate_pool.html"


@method_decorator(login_required, name="dispatch")
@method_decorator(
    permission_required(perm="recruitment.view_candidate"), name="dispatch"
)
class CandidatePoolListView(HorillaListView):
    """
    The Candidate Pool table: the six PRD columns.

    Filtering, sorting and pagination are the framework's (sortby_key /
    CandidatePoolFilter), applied to a company-scoped queryset.
    """

    model = Candidate
    filter_class = CandidatePoolFilter
    view_id = "candidate-pool-container"

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.search_url = reverse("candidate-pool-list")

    columns = [
        (_("Number"), "pool_number"),
        (_("Candidate Name"), "get_full_name", "get_avatar"),
        (_("Job Applied To"), "pool_job_applied_to"),
        (_("Date Applied"), "pool_date_applied"),
        (_("Current Status"), "pool_status"),
        (_("Contact Verification"), "pool_contact_verification"),
    ]
    default_columns = columns

    #: Every column sorts (PRD: search / filter / sort).
    sortby_mapping = [
        (_("Number"), "pk"),
        (_("Candidate Name"), "name"),
        (_("Job Applied To"), "pool_job_applied_to"),
        (_("Date Applied"), "created_at"),
        (_("Current Status"), "pool_status"),
        (_("Contact Verification"), "pool_contact_verification"),
    ]

    #: Clicking a row opens Candidate Details (PRD).
    row_attrs = """
        class="cursor-pointer"
        onclick="window.location.href='{get_individual_url}'"
    """

    #: Open Candidate Details / Map to Job Opening. Both the detail page and
    #: the mapping service already existed; without a row action neither was
    #: reachable from this table.
    action_method = "pool_actions"

    #: No lifecycle field is bulk-editable: the generic bulk update writes with
    #: queryset.update(), which bypasses save() and would skip the
    #: hired/canceled derivation and every audit event.
    bulk_update_fields = []

    def get_queryset(self, *args, **kwargs):
        """
        Narrow the framework's queryset to the user's companies.

        super() MUST run first. HorillaListView.get_queryset() is guarded by
        ``if not self.queryset:`` and, inside that block, initialises
        ``self._saved_filters`` plus the filter/sort/pagination state that
        get_context_data() later requires. Pre-assigning self.queryset (the
        obvious way to inject a scoped queryset) skips the whole block and the
        view then dies with AttributeError: no attribute '_saved_filters'.

        Narrowing afterwards is not merely a reordering: super() builds from
        Candidate.objects, whose company filter passes company_id__isnull=True
        rows through to EVERY tenant. Intersecting with the explicitly allowed
        company ids is what actually excludes an unscoped candidate from every
        company's pool.
        """
        queryset = super().get_queryset(*args, **kwargs)
        allowed = candidate_service.accessible_candidates(self.request.user)
        # select_related lives in accessible_candidates, so the job-opening and
        # stage columns cost no query per row.
        self.queryset = queryset.filter(
            pk__in=allowed.values("pk")
        ).select_related(
            "recruitment_id",
            "stage_id",
            "job_position_id",
            "company_id",
            # Current Status checks for a rejection on every row.
            "rejected_candidate",
        )
        return self.queryset


@method_decorator(login_required, name="dispatch")
@method_decorator(
    permission_required(perm="recruitment.view_candidate"), name="dispatch"
)
class CandidatePoolNavView(HorillaNavView):
    """Search, filters and export for the Candidate Pool."""

    nav_title = _("Candidate Pool")
    filter_form_context_name = "form"
    filter_instance_context_name = "f"
    filter_body_template = "cbv/candidate_pool/filter.html"
    search_swap_target = "#listContainer"

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.search_url = reverse("candidate-pool-list")
        self.filter_instance = CandidatePoolFilter()
        if self.request.user.has_perm("recruitment.add_candidate"):
            self.create_attrs = f"""
                hx-get="{reverse('candidate-pool-add')}"
                hx-target="#genericModalBody"
                data-target="#genericModal"
                data-toggle="oh-modal-toggle"
            """
        self.actions = []
        # Export is offered only to a user who actually holds
        # export_candidate. The service re-checks it server-side, so hiding the
        # control is convenience, never the boundary.
        if self.request.user.has_perm(
            "recruitment.export_candidate"
        ) or self.request.user.is_superuser:
            # Downloads the six Pool columns for the current search + filter.
            export_url = reverse("candidate-pool-export")
            self.actions.append(
                {
                    "action": _("Export"),
                    "attrs": (
                        'onclick="'
                        "var p = new URLSearchParams("
                        "$('#applyFilter').closest('form').serialize());"
                        "var s = $('input[name=search]').first().val();"
                        "if (s) p.set('search', s);"
                        f"window.location.href = '{export_url}?' + p.toString();"
                        '" style="cursor: pointer;"'
                    ),
                }
            )

    search_in = [
        ("name", _("Candidate Name")),
        ("email", _("Email")),
        ("recruitment_id__title", _("Job Opening")),
    ]


@method_decorator(login_required, name="dispatch")
@method_decorator(
    permission_required(perm="recruitment.add_candidate"), name="dispatch"
)
class PoolCandidateFormView(HorillaFormView):
    """
    "+ Add Candidate" on the Candidate Pool: a candidate with no job opening.

    Created through the candidate service, which scopes the company and records
    the audit event.
    """

    form_class = PoolCandidateForm
    model = Candidate
    new_display_title = _("Add Candidate")

    def form_valid(self, form):
        from django.contrib import messages

        from recruitment.services.errors import RecruitmentError

        if not form.is_valid():
            return super().form_valid(form)
        try:
            candidate_service.create_candidate(
                self.request.user,
                name=form.cleaned_data["name"],
                email=form.cleaned_data["email"],
                mobile=form.cleaned_data["mobile"],
                resume=form.cleaned_data["resume"],
            )
        except RecruitmentError as error:
            form.add_error(None, str(error))
            return self.form_invalid(form)
        messages.success(self.request, _("Candidate added to the pool."))
        return self.HttpResponse(targets_to_reload=["#applyFilter"])
