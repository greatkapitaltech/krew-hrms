"""
recruitment/cbv/pipeline.py
"""

from typing import Any

from django.contrib import messages
from django.core.cache import cache as CACHE
from django.urls import reverse, reverse_lazy
from django.utils.decorators import method_decorator
from django.utils.http import urlencode
from django.utils.translation import gettext_lazy as _

from horilla.decorators import hx_request_required
from horilla_views.cbv_methods import login_required
from horilla_views.generic.cbv.kanban import HorillaKanbanView
from horilla_views.generic.cbv.views import (
    HorillaFormView,
    HorillaListView,
    HorillaNavView,
    HorillaTabView,
    TemplateView,
    get_short_uuid,
)
from recruitment import filters, forms, models
from recruitment.cbv_decorators import manager_can_enter
from recruitment.templatetags.recruitmentfilters import (
    recruitment_manages,
    stage_manages,
)


def _pipeline_cache_key(request):
    return request.session.session_key + "pipeline"


def _pipeline_filters(request):
    """
    The pipeline page's filters, as cached for its stage/candidate requests.

    Only the query parameters are cached. Upstream cached the querysets
    themselves, and pickling a QuerySet evaluates it -- every stage-column
    request loaded and serialised every matching candidate in the company.
    """
    return CACHE.get(_pipeline_cache_key(request)) or {}


def _pipeline_stages(request, filter_class):
    params = _pipeline_filters(request).get("stage_params")
    return filter_class(params if params is not None else request.GET).qs.order_by(
        "sequence"
    )


def _pipeline_candidates(request, filter_class):
    params = _pipeline_filters(request).get("candidate_params")
    return filter_class(params if params is not None else request.GET).qs.filter(
        is_active=True
    )


@method_decorator(login_required, name="dispatch")
@method_decorator(
    manager_can_enter(perm="recruitment.view_recruitment"), name="dispatch"
)
class PipelineView(TemplateView):
    """
    PipelineView
    """

    template_name = "cbv/pipeline/pipeline.html"


@method_decorator(login_required, name="dispatch")
@method_decorator(
    manager_can_enter(perm="recruitment.view_recruitment"), name="dispatch"
)
class RecruitmentTabView(HorillaTabView):
    """
    RecruitmentTabView
    """

    filter_class = filters.RecruitmentFilter

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # DRAFT and REVIEW openings are not live recruitment: they have no
        # applicants yet and their question set is not frozen, so their pipeline
        # must not be offered here. The existing is_active / closed=False
        # conditions are left exactly as they were, so which openings count as
        # current is otherwise unchanged. A deep link
        # (cbv-pipeline/?obj_id=<pk>) to a DRAFT opening therefore simply
        # matches no tab rather than exposing its pipeline.
        from recruitment.services.job_opening import pipeline_hidden_statuses

        recruitments = (
            self.filter_class(self.request.GET)
            .qs.filter(is_active=True, closed=False)
            .exclude(status__in=pipeline_hidden_statuses())
        )
        view_type = self.request.GET.get("view")
        if not view_type and self.request.user and self.request.user.is_authenticated:
            # ActiveView lives in horilla_views.models and Q in
            # django.db.models. Neither is on recruitment.models, which
            # `models` is bound to in this module (see the imports above), so
            # qualifying them that way raised AttributeError on every
            # pipeline-tab request without ?view= -- i.e. the normal path in
            # from the sidebar, which 500'd the whole pipeline screen.
            # horilla_views' own views use these same names correctly.
            from django.db.models import Q
            from horilla_views.models import ActiveView

            active_view = (
                ActiveView.objects.filter(created_by=self.request.user)
                .filter(Q(path=self.request.path) | Q(path=reverse("cbv-pipeline")))
                .first()
            )
            if active_view and active_view.type:
                view_type = active_view.type
        if not view_type:
            view_type = "card"
        # CACHE.set(
        #     self.request.session.session_key + "pipeline",
        #     {
        #         "stages": GetStages.filter_class(self.request.GET).qs.order_by(
        #             "sequence"
        #         ),
        #         "recruitments": recruitments,
        #         "candidates": False,
        #     },
        #     timeout=600,
        # )
        # Filters only (see _pipeline_filters); candidate filters are taken
        # from the first stage request, as before.
        CACHE.set(
            _pipeline_cache_key(self.request),
            {"stage_params": self.request.GET.copy(), "candidate_params": None},
            timeout=600,
        )
        self.tabs = []
        view_perm = self.request.user.has_perm("recruitment.view_recruitment")
        change_perm = self.request.user.has_perm("recruitment.change_recruitment")
        add_stage_perm = self.request.user.has_perm("recruitment.add_stage")
        # Only used by the hidden Resume Shortlisting / Delete tab actions below.
        # add_cand_perm = self.request.user.has_perm("recruitment.add_candidate")
        # delete_perm = self.request.user.has_perm("recruitment.delete_recruitment")
        stage_qs = GetStages.filter_class(self.request.GET).qs
        for rec in recruitments:
            rec_manager_perm = recruitment_manages(self.request.user, rec)
            stage_manage_perm = stage_manages(self.request.user, rec)
            tab = {}
            tab["title"] = rec
            url = reverse("candidate-card-cbv", kwargs={"pk": rec.pk})

            if view_type == "list":
                url = (
                    reverse("get-stages-recruitment", kwargs={"rec_id": rec.pk})
                    + f"?view={view_type}"
                )
            tab["url"] = url

            self.query_params["view"] = view_type
            tab["badge_label"] = _("Stages")
            tab["badge"] = stage_qs.filter(recruitment_id=rec.pk).count()
            tab["actions"] = []
            if rec_manager_perm or change_perm:
                if add_stage_perm or rec_manager_perm or change_perm:
                    tab["actions"].append(
                        {
                            "action": _("Add Stage"),
                            "attrs": f"""
                                data-toggle="oh-modal-toggle"
                                data-target="#genericModal"
                                hx-get="{reverse('rec-stage-create')}?recruitment_id={rec.pk}"
                                hx-target="#genericModalBody"
                                style="cursor: pointer;"
                            """,
                        },
                    )

                if change_perm or rec_manager_perm:
                    tab["actions"].append(
                        {
                            "action": _("Edit"),
                            "attrs": f"""
                                data-toggle="oh-modal-toggle"
                                data-target="#genericModal"
                                hx-get="{reverse("recruitment-update-pipeline", kwargs={"pk": rec.pk})}"
                                hx-target="#genericModalBody"
                                style="cursor: pointer;"
                            """,
                        },
                    )

                if add_stage_perm or rec_manager_perm or change_perm:
                    tab["actions"].append(
                        {
                            "action": _("Manage Stage Order"),
                            "attrs": f"""
                                data-toggle="oh-modal-toggle"
                                data-target="#genericModal"
                                hx-get="{reverse("rec-update-stage-seq", kwargs={"pk": rec.pk})}"
                                hx-target="#genericModalBody"
                                style="cursor: pointer;"
                            """,
                        }
                    )

                # Close is offered only while the opening is PUBLISHED.
                # There is no Reopen action: CLOSED is terminal, and the
                # supported way to run the role again is Duplicate.
                # Hidden per PRD: Close is automatic at the End Date only; the one
                # manual take-down is Remove (Job Openings list).
                # if (
                    # change_perm or rec_manager_perm
                # ) and rec.status == models.Recruitment.Status.PUBLISHED:
                    # tab["actions"].append(
                        # {
                            # "action": _("Close"),
                            # "attrs": f"""
                                # href="{reverse("recruitment-close-pipeline", kwargs={"rec_id": rec.pk})}"
                                # style="cursor: pointer;"
                                # onclick="return confirm('Are you sure you want to close this job opening? This cannot be undone.');"
                            # """,
                        # },
                    # )
                # Hidden per PRD: Resume Shortlisting (resume scoring) is Phase 2.
                # if add_cand_perm or rec_manager_perm or change_perm:
                #     tab["actions"].append(
                #         {
                #             "action": _("Resume Shortlisting"),
                #             "attrs": f"""
                #                 data-toggle="oh-modal-toggle"
                #                 data-target="#bulkResumeUpload"
                #                 hx-get="{reverse('view-bulk-resume')}?rec_id={rec.pk}"
                #                 hx-target="#bulkResumeUploadBody"
                #                 style="cursor: pointer;"
                #             """,
                #         },
                #     )
                # Hidden per PRD: job openings are never hard-deleted; Remove archives them.
                # if delete_perm:
                #     tab["actions"].append(
                #         {
                #             "action": _("Delete"),
                #             "attrs": f"""
                #                 data-toggle="oh-modal-toggle"
                #                 data-target="#deleteConfirmation"
                #                 hx-get="{reverse('generic-delete')}?model=recruitment.Recruitment&pk={rec.pk}"
                #                 hx-target="#deleteConfirmationBody"
                #                 style="cursor: pointer;"
                #             """,
                #         }
                #     )
            if stage_manage_perm or view_perm:
                self.tabs.append(tab)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["show_filter_tags"] = True

        return context


@method_decorator(login_required, name="dispatch")
@method_decorator(hx_request_required, name="dispatch")
@method_decorator(
    manager_can_enter(perm="recruitment.view_recruitment"), name="dispatch"
)
class GetStages(TemplateView):
    """
    GetStages
    """

    filter_class = filters.StageFilter

    template_name = "cbv/pipeline/stages.html"
    stages = None

    def get(self, request, *args, **kwargs):
        """
        get method
        """
        rec_id = kwargs["rec_id"]

        # The tab list already excludes DRAFT and REVIEW, so in the normal flow
        # this endpoint is only ever asked for an eligible opening. Requested
        # directly it would still have rendered a draft opening's stages, so the
        # same rule is enforced here from the one shared definition.
        #
        # Reported as not-found rather than forbidden, matching how the rest of
        # recruitment answers for an object that is out of scope -- and the
        # company-scoped manager makes another tenant's opening resolve to None
        # here too.
        from django.http import Http404

        from recruitment.services.job_opening import appears_in_pipeline

        opening = models.Recruitment.objects.filter(pk=rec_id).first()
        if not appears_in_pipeline(opening):
            raise Http404("No job opening pipeline found matching the query.")

        # cache = CACHE.get(request.session.session_key + "pipeline")
        # if cache is None:
        #     cache = {
        #         "stages": self.filter_class(request.GET).qs.order_by("sequence"),
        #         "candidates": False,
        #     }
        #     CACHE.set(request.session.session_key + "pipeline", cache, timeout=600)
        # if not cache.get("candidates"):
        #     cache["candidates"] = CandidateList.filter_class(
        #         self.request.GET
        #     ).qs.filter(is_active=True)
        #     CACHE.set(request.session.session_key + "pipeline", cache)
        #
        # self.stages = cache["stages"].filter(recruitment_id=rec_id)
        cache = CACHE.get(_pipeline_cache_key(request))
        if cache is None:
            cache = {"stage_params": request.GET.copy(), "candidate_params": None}
        if cache.get("candidate_params") is None:
            cache["candidate_params"] = request.GET.copy()
        CACHE.set(_pipeline_cache_key(request), cache, timeout=600)

        self.stages = _pipeline_stages(request, self.filter_class).filter(
            recruitment_id=rec_id
        )
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        stages_list = list(self.stages)
        # cache_key = self.request.session.session_key + "pipeline"
        # cache = CACHE.get(cache_key) or {}
        # candidates_qs = cache.get("candidates")
        # if candidates_qs is False or candidates_qs is None:
        #     candidates_qs = CandidateList.filter_class(self.request.GET).qs.filter(
        #         is_active=True
        #     )
        candidates_qs = _pipeline_candidates(self.request, CandidateList.filter_class)

        from django.db.models import Count

        counts = (
            candidates_qs.filter(stage_id__in=[s.id for s in stages_list])
            .values("stage_id")
            .annotate(total=Count("id"))
        )
        count_map = {item["stage_id"]: item["total"] for item in counts}
        for stage in stages_list:
            stage.candidate_count = count_map.get(stage.id, 0)

        context["stages"] = stages_list
        context["view_id"] = get_short_uuid(6, "hsv")
        context["rec_id"] = kwargs["rec_id"]
        return context


@method_decorator(login_required, name="dispatch")
@method_decorator(
    manager_can_enter(perm="recruitment.view_recruitment"), name="dispatch"
)
class CandidateList(HorillaListView):
    """
    CandidateList
    """

    model = models.Candidate
    filter_class = filters.CandidateFilter
    filter_selected = False
    quick_export = False
    next_prev = False
    show_filter_tags = True
    filter_keys_to_remove = ["rec_id", "obj_id"]
    records_per_page = 10
    records_count_in_tab = False

    custom_empty_template = "cbv/pipeline/empty.html"
    header_attrs = {
        "mobile": """ style="width:100px;" """,
        "Stage": """ style="width:100px;" """,
        "get_interview_count": """ style="width:200px;" """,
        "option": """ style="width:280px !important" """,
    }
    columns = [
        (_("Name"), "candidate_name", "get_avatar"),
        (_("Email"), "mail_indication"),
        (_("Stage"), "stage_drop_down"),
        # Hidden per PRD: Ratings are Phase 2; interviews are not tracked in-app.
        # (_("Rating"), "rating_bar"),
        # (_("Scheduled Interview"), "get_interview_count"),
        (_("Hired Date"), "hired_date"),
        (_("Job Position"), "job_position_id__job_position"),
        (_("Contact"), "mobile"),
    ]

    export_columns = [
        (_("Name"), "candidate_name", "get_avatar"),
        (_("Email"), "mail_indication"),
        (_("Stage"), "stage_id"),
        # (_("Rating"), "get_avg_rating"),
        # (_("Scheduled Interview"), "get_total_interview"),
        (_("Hired Date"), "hired_date"),
        (_("Job Position"), "job_position_id__job_position"),
        (_("Contact"), "mobile"),
    ]

    default_columns = [
        (_("Name"), "candidate_name", "get_avatar"),
        (_("Email"), "mail_indication"),
        (_("Stage"), "stage_drop_down"),
    ]

    # bulk_update_fields = [
    #     "stage_id",
    #     "hired_date",
    # ]
    # Not in the PRD: the generic bulk "Update" wrote stage_id straight to the
    # database, skipping the handoff gate, forward-only rule, History and the
    # rejection email. The PRD's bulk actions are Bulk Move Forward / Bulk
    # Reject (bulk_actions below), which go through the services.
    bulk_update_fields = []

    #: The PRD's bulk pipeline actions, for clearing a stage after a screening
    #: round. They skip the per-candidate remark (one remark pasted onto fifty
    #: people is not accountability) but nothing else: the services still
    #: authorize, enforce the direction rule, email each rejected candidate and
    #: audit every row. Candidates the user may not move are reported back
    #: rather than silently skipped.
    bulk_actions = [
        {
            "action": _("Move Forward"),
            "url": reverse_lazy("candidates-bulk-move-forward"),
            "class": "border-success text-success",
            "confirm": _(
                "Move the selected candidates to their next stage? "
                "No remark is recorded for a bulk move."
            ),
        },
        {
            "action": _("Reject"),
            "url": reverse_lazy("candidates-bulk-reject"),
            "class": "border-danger text-danger",
            "confirm": _(
                "Reject the selected candidates? Each one is emailed "
                "automatically, and rejection cannot be undone."
            ),
        },
    ]

    row_attrs = """
        class="cursor-pointer"
        onclick="window.location.href = '{get_profile_url}?next=' + encodeURIComponent(window.location.pathname + window.location.search)"
    """

    actions = [
        {
            # The PRD's primary pipeline action: advance one candidate, with a
            # mandatory remark. The destination is the pipeline's next stage,
            # decided server-side -- not picked here.
            "action": _("Move Forward"),
            "accessibility": "recruitment.cbv.accessibility.move_forward_accessibility",
            "icon": "arrow-forward-circle-outline",
            "attrs": """
                class="oh-btn oh-btn--light-bkg oh-btn--sq-sm"
                hx-get = "{get_move_forward_url}"
                data-toggle="oh-modal-toggle"
                data-target="#genericModal"
                hx-target="#genericModalBody"
            """,
        },
        {
            # Manager only (PRD); one stage back with a mandatory remark.
            "action": _("Move Backward"),
            "icon": "arrow-back-circle-outline",
            "accessibility": "recruitment.cbv.accessibility.move_backward_accessibility",
            "attrs": """
                class="oh-btn oh-btn--light-bkg oh-btn--sq-sm"
                hx-get = "{get_move_backward_url}"
                data-toggle="oh-modal-toggle"
                data-target="#genericModal"
                hx-target="#genericModalBody"
            """,
        },
        {
            # Opens a prefilled Google Calendar event (PRD): the invite is
            # created in the team's own calendar, where Meet and the
            # candidate's invitation come from. The in-app Interview module
            # remains available from the sidebar for recording interviews.
            "action": _("Schedule Interview"),
            "accessibility": "recruitment.cbv.accessibility.schedule_interview_accessibility",
            "icon": "time-outline",
            "attrs": """
                class="oh-btn oh-btn--light-bkg oh-btn--sq-sm"
                href="{get_interview_schedule_url}"
                target="_blank"
                rel="noopener noreferrer"
            """,
        },
        {
            # The Meet link fetched from the Google event (InterviewMeeting).
            "action": _("Join Meeting"),
            "accessibility": "recruitment.cbv.accessibility.join_meeting_accessibility",
            "icon": "videocam-outline",
            "attrs": """
                class="oh-btn oh-btn--light-bkg oh-btn--sq-sm"
                href="{get_join_meeting_url}"
                target="_blank"
                rel="noopener noreferrer"
            """,
        },
        {
            # Look the Google event up now instead of waiting for the sync.
            "action": _("Fetch Meeting Link"),
            "accessibility": "recruitment.cbv.accessibility.fetch_meeting_accessibility",
            "icon": "refresh-outline",
            "attrs": """
                class="oh-btn oh-btn--light-bkg oh-btn--sq-sm"
                hx-post="{get_interview_meeting_refresh_url}"
                hx-swap="none"
            """,
        },
        {
            # Ad-hoc correspondence opens in the recruiter's own mail app
            # (mailto:), so the reply lands in their inbox.
            "action": _("Email Candidate"),
            "accessibility": "recruitment.cbv.accessibility.email_candidate_accessibility",
            "icon": "mail-open-outline",
            "attrs": """
                class="oh-btn oh-btn--light-bkg oh-btn--sq-sm"
                href="{get_email_compose_url}"
            """,
        },
        {
            "action": _("Reject"),
            "accessibility": "recruitment.cbv.accessibility.stage_action_accessibility",
            "icon": "thumbs-down-outline",
            "attrs": """
                class="oh-btn oh-btn--light-bkg oh-btn--sq-sm"
                {rejected_candidate_class}
                onclick="event.preventDefault(); event.stopPropagation(); krewConfirmReject('{get_rejected_candidate_url}');"
            """,
        },
        # Hidden per PRD (Send Mail / Talent Pool / Document Request are not in MVP; notes and resume live in Candidate Details). Kept for reference:
        # {
        #     "action": _("Send Mail"),
        #     "icon": "mail-open-outline",
        #     "attrs": """
        #         class="oh-btn oh-btn--light-bkg oh-btn--sq-sm"
        #         hx-get = "{get_send_mail}"
        #         data-toggle="oh-modal-toggle"
        #         data-target="#objectDetailsModal"
        #         hx-target="#objectDetailsModalTarget"
        #     """,
        # },
        # {
        #     "action": _("Add to Talent Pool"),
        #     "icon": "heart-circle-outline",
        #     "attrs": """
        #         class="oh-btn oh-btn--light-bkg oh-btn--sq-sm disabled"
        #         data-toggle="oh-modal-toggle"
        #         hx-get="{get_skill_zone_url}"
        #         data-target="#genericModal"
        #         hx-target="#genericModalBody"
        #     """,
        # },
        # {
        #     "action": _("Document Request"),
        #     "icon": "document-attach-outline",
        #     "attrs": """
        #         hx-get="{get_document_request}"
        #         data-target="#genericModal"
        #         hx-target="#genericModalBody"
        #         class="oh-btn oh-btn--light-bkg oh-btn--sq-sm"
        #         data-toggle="oh-modal-toggle"
        #     """,
        # },
        # {
        #     "action": _("View Note"),
        #     "icon": "newspaper-outline",
        #     "attrs": """
        #         class="oh-btn oh-btn--light-bkg oh-btn--sq-sm oh-activity-sidebar__open"
        #         hx-get="{get_view_note_url}"
        #         data-target="#activitySidebar"
        #         hx-target="#activitySidebar"
        #         onclick="$('#activitySidebar').addClass('oh-activity-sidebar--show')"
        #     """,
        # },
        # 
    ]

    def get_bulk_form(self):
        form = super().get_bulk_form()
        form.fields["stage_id"].queryset = form.fields["stage_id"].queryset.filter(
            recruitment_id=self.kwargs["rec_id"]
        )
        return form

    def bulk_update_accessibility(self):
        """
        Bulk Update accessiblity
        """
        if not self.kwargs.get("stage_id"):
            return super().bulk_update_accessibility()
        first_cand_in_stage = self.queryset.first()
        return super().bulk_update_accessibility() or (
            first_cand_in_stage
            and (
                self.request.user.employee_get
                in first_cand_in_stage.stage_id.stage_managers.all()
                or self.request.user.employee_get
                in first_cand_in_stage.recruitment_id.recruitment_managers.all()
            )
        )

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.search_url = self.request.path

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if not self.bulk_update_accessibility():
            context["actions"] = []
        # PRD: Bulk Move Forward / Bulk Reject are drive-level (Manager only).
        from recruitment.services.authorization import (
            has_company_wide_job_opening_authority,
            manages_job_opening,
        )

        user = self.request.user
        opening = models.Recruitment.objects.filter(pk=self.kwargs.get("rec_id")).first()
        if not (
            user.is_superuser
            or has_company_wide_job_opening_authority(user)
            or (opening is not None and manages_job_opening(user, opening))
        ):
            context["bulk_actions"] = []
        return context

    def get(self, request, *args, **kwargs):
        self.selected_instances_key_id = f"selectedCandidateRecords{kwargs['stage_id']}"
        return super().get(request, *args, **kwargs)

    def get_queryset(self, *args, **kwargs):
        if self.queryset is None:
            # cache_key = self.request.session.session_key + "pipeline"
            # cache = CACHE.get(cache_key)
            # if cache is None:
            #     cache = {
            #         "stages": filters.StageFilter(self.request.GET).qs.order_by(
            #             "sequence"
            #         ),
            #         "candidates": False,
            #     }
            # if not cache.get("candidates"):
            #     cache["candidates"] = self.filter_class(self.request.GET).qs.filter(
            #         is_active=True
            #     )
            # CACHE.set(cache_key, cache, timeout=600)
            #
            # queryset = cache["candidates"].filter(stage_id=self.kwargs["stage_id"])
            # Load each card's opening/stage/position/rejection with the list,
            # not one query per card.
            queryset = (
                _pipeline_candidates(self.request, self.filter_class)
                .filter(stage_id=self.kwargs["stage_id"])
                .select_related(
                    "recruitment_id",
                    "stage_id",
                    "job_position_id",
                    "company_id",
                    "rejected_candidate",
                )
            )
            super().get_queryset(queryset=queryset, filtered=True)

        return self.queryset


@method_decorator(login_required, name="dispatch")
@method_decorator(
    manager_can_enter(perm="recruitment.view_recruitment"), name="dispatch"
)
class CandidateCard(HorillaKanbanView):
    model = models.Candidate
    filter_class = filters.CandidateFilter
    group_filter_class = filters.StageFilter
    group_key = "stage_id"
    records_per_page = 10
    filter_keys_to_remove = ["rec_id", "obj_id"]
    #: No drag-and-drop (PRD): stage changes go through Move Forward / Move
    #: Backward, which apply the movement rules, the handoff gate and History.
    drag_enabled = False
    group_label_key = "stage"

    kanban_attrs = """
        onclick="window.location.href = '{get_profile_url}?next=' + encodeURIComponent(window.location.pathname + window.location.search)"
    """

    details = {
        "image_src": "{get_avatar}",
        "title": "{get_full_name}",
        "email": "{email}",
        "position": "{job_position_id__job_position}",
    }

    group_actions = [
        {
            "action": _("Add Candidate"),
            "accessibility": "recruitment.accessibility.add_candidate_accessibility",
            "attrs": """
                hx-target="#objectCreateModalTarget"
                hx-get="{get_add_candidate_url}"
                data-toggle="oh-modal-toggle"
                data-target="#objectCreateModal"
            """,
        },
        {
            "action": _("Edit"),
            "accessibility": "recruitment.accessibility.edit_stage_accessibility",
            "attrs": """
                hx-target="#genericModalBody"
                hx-get="{get_stage_update_url}"
                data-toggle="oh-modal-toggle"
                data-target="#genericModal"
            """,
        },
        {
            "action": _("Edit Managers"),
            "accessibility": "recruitment.accessibility.edit_fixed_stage_managers_accessibility",
            "attrs": """
                hx-target="#genericModalBody"
                hx-get="{get_stage_managers_update_url}"
                data-toggle="oh-modal-toggle"
                data-target="#genericModal"
            """,
        },
        {
            "action": _("Delete"),
            "accessibility": "recruitment.accessibility.delete_stage_accessibility",
            "attrs": """
                hx-target="#deleteConfirmationBody"
                hx-get="{get_delete_url}"
                data-toggle="oh-modal-toggle"
                data-target="#deleteConfirmation"
            """,
        },
        # Hidden per PRD (Bulk Mail is not in MVP). Kept for reference:
        # {
        #     "action": _("Bulk Mail"),
        #     "accessibility": "recruitment.accessibility.edit_stage_accessibility",
        #     "attrs": """
        #         hx-target="#objectCreateModalTarget"
        #         hx-get="{get_send_email_url}"
        #         data-toggle="oh-modal-toggle"
        #         data-target="#objectCreateModal"
        #     """,
        # },
    ]

    actions = [
        {
            "action": _("Move Forward"),
            "accessibility": "recruitment.cbv.accessibility.move_forward_accessibility",
            "attrs": """
                hx-get = "{get_move_forward_url}"
                data-toggle="oh-modal-toggle"
                data-target="#genericModal"
                hx-target="#genericModalBody"
            """,
        },
        {
            "action": _("Move Backward"),
            "accessibility": "recruitment.cbv.accessibility.move_backward_accessibility",
            "attrs": """
                hx-get = "{get_move_backward_url}"
                data-toggle="oh-modal-toggle"
                data-target="#genericModal"
                hx-target="#genericModalBody"
            """,
        },
        {
            "action": _("Schedule Interview"),
            "accessibility": "recruitment.cbv.accessibility.schedule_interview_accessibility",
            "attrs": """
                href="{get_interview_schedule_url}"
                target="_blank"
                rel="noopener noreferrer"
            """,
        },
        {
            "action": _("Join Meeting"),
            "accessibility": "recruitment.cbv.accessibility.join_meeting_accessibility",
            "attrs": """
                href="{get_join_meeting_url}"
                target="_blank"
                rel="noopener noreferrer"
            """,
        },
        {
            "action": _("Fetch Meeting Link"),
            "accessibility": "recruitment.cbv.accessibility.fetch_meeting_accessibility",
            "attrs": """
                hx-post="{get_interview_meeting_refresh_url}"
                hx-swap="none"
            """,
        },
        {
            "action": _("Email Candidate"),
            "accessibility": "recruitment.cbv.accessibility.email_candidate_accessibility",
            "attrs": """
                href="{get_email_compose_url}"
            """,
        },
        {
            "action": _("Reject"),
            "accessibility": "recruitment.cbv.accessibility.stage_action_accessibility",
            "attrs": """
                class="oh-dropdown__link"
                onclick="event.preventDefault(); event.stopPropagation(); krewConfirmReject('{get_add_to_reject}');"
            """,
        },
        # "Edit Rejected Candidate" deliberately removed from the pipeline:
        # rejection is terminal and carries an email to the candidate, so
        # re-opening the reason after the fact invites editing a decision that
        # has already been communicated. The reason stays visible in History
        # and on the Rejected Candidates screen.
        # Hidden per PRD (candidates are never archived or deleted; Talent Pool, self tracking and document requests are out of MVP). Kept for reference:
        # {
        #     "action": _("Send Mail"),
        #     "attrs": """
        #         hx-get = "{get_send_mail}"
        #         data-toggle="oh-modal-toggle"
        #         data-target="#objectDetailsModal"
        #         hx-target="#objectDetailsModalTarget"
        #     """,
        # },
        # {
        #     "action": "Add to Talent Pool",
        #     "accessibility": "recruitment.cbv.accessibility.add_skill_zone",
        #     "attrs": """
        #         data-toggle="oh-modal-toggle"
        #         data-target="#genericModal"
        #         hx-get="{get_add_to_skill}"
        #         hx-target="#genericModalBody"
        #         class="oh-dropdown__link"
        # 
        #     """,
        # },
        # {
        #     "action": "View candidate self tracking",
        #     "accessibility": "recruitment.cbv.accessibility.check_candidate_self_tracking",
        #     "attrs": """
        #         href="{get_self_tracking_url}"
        #         class="oh-dropdown__link"
        #     """,
        # },
        # {
        #     "action": "Request Document",
        #     "accessibility": "recruitment.cbv.accessibility.request_document",
        #     "attrs": """
        #         data-toggle="oh-modal-toggle"
        #         data-target="#genericModal"
        #         hx-get="{get_document_request_doc}"
        #         hx-target="#genericModalBody"
        #         class="oh-dropdown__link"
        #     """,
        # },
        # {
        #     "action": _("View Note"),
        #     "attrs": """
        #         hx-get="{get_view_note_url}"
        #         data-target="#activitySidebar"
        #         hx-target="#activitySidebar"
        #         onclick="$('#activitySidebar').addClass('oh-activity-sidebar--show')"
        #     """,
        # },
        # {
        #     "action": _("Resume"),
        #     "attrs": """
        #         href="{get_resume_url}" target="_blank"
        #     """,
        # },
        # {
        #     "action": "archive_status",
        #     "attrs": """
        #         class="oh-dropdown__link"
        #         onclick="archiveCandidate({get_archive_url});"
        #     """,
        # },
        # 
    ]

    def get_related_groups(self, *args, **kwargs):
        related_groups = super().get_related_groups(*args, **kwargs)
        rec_id = self.kwargs.get("pk")
        if rec_id:
            related_groups = related_groups.filter(recruitment_id=rec_id)

        return related_groups


@method_decorator(login_required, name="dispatch")
@method_decorator(
    manager_can_enter(perm="recruitment.view_recruitment"), name="dispatch"
)
class PipelineNav(HorillaNavView):
    """
    HorillaNavView
    """

    search_url = reverse_lazy("cbv-pipeline-tab")
    nav_title = _("Pipeline")
    search_swap_target = "#pipelineContainer"
    filter_body_template = "cbv/pipeline/pipeline_filter.html"
    filter_instance = filters.RecruitmentFilter()
    filter_form_context_name = "form"
    apply_first_filter = False

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        if self.request.user.has_perm("recruitment.add_recruitment"):
            self.create_attrs = f"""
                hx-get="{reverse_lazy('recruitment-create')}?{urlencode({'pipeline': 'true'})}"
                hx-target="#genericModalBody"
                data-target="#genericModal"
                data-toggle="oh-modal-toggle"
            """
        else:
            self.create_attrs = None

        rec_id = self.request.GET.get("obj_id", "")
        id_suffix = f"&obj_id={rec_id}" if rec_id else ""
        self.view_types = [
            {
                "type": "list",
                "icon": "list-outline",
                "url": f'{reverse_lazy("cbv-pipeline-tab")}?view=list{id_suffix}',
                "attrs": f"""
                    title ='List'
                """,
            },
            {
                "type": "card",
                "icon": "grid-outline",
                "url": f'{reverse_lazy("cbv-pipeline-tab")}?view=card{id_suffix}',
                "attrs": f"""
                    title ='Card'
                """,
            },
        ]

    def get_context_data(self, **kwargs):
        """
        context data
        """
        context = super().get_context_data(**kwargs)
        stage_filter_obj = GetStages.filter_class()
        candidate_filter_obj = CandidateList.filter_class()
        context["stage_filter_obj"] = stage_filter_obj
        context["candidate_filter_obj"] = candidate_filter_obj
        return context


@method_decorator(login_required, name="dispatch")
@method_decorator(
    manager_can_enter(perm="recruitment.view_recruitment"), name="dispatch"
)
class ChangeStage(HorillaFormView):
    """
    Change Candidate stage
    """

    model = models.Candidate
    form_class = forms.StageChangeForm

    def form_valid(self, form):
        """
        The Stage dropdown. Goes through the candidate service like every other
        move, so the PRD rules apply (Stage Managers forward-only, Hired only
        via the handoff form) and the move is recorded in History. Saving the
        form directly skipped all of that.
        """
        from recruitment.services import candidate as candidate_service
        from recruitment.services.errors import RecruitmentError

        stage = form.cleaned_data.get("stage_id") if form.is_valid() else None
        if stage is None:
            messages.info(self.request, _("Stage not updated"))
            return self.HttpResponse()
        try:
            candidate_service.move_to_stage(
                self.request.user, self.kwargs.get("pk"), stage.pk
            )
        except RecruitmentError as error:
            messages.error(self.request, str(error))
            return self.HttpResponse(targets_to_reload=["#applyFilter"])
        messages.success(self.request, _("Stage Updated"))
        return self.HttpResponse()
        # Original upstream body, replaced because it skipped the movement
        # rules, the hiring handoff and History:
        # if form.is_valid():
        #     messages.success(self.request, _("Stage Updated"))
        #     form.save()
        #     return self.HttpResponse()
        # messages.info(self.request, _("Stage not updated"))
        # return self.HttpResponse()
