"""
horilla_api/api_views/recruitment/views.py
"""

from django.contrib.auth.models import AnonymousUser
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext_lazy as _
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import status
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from base.methods import filtersubordinates
from horilla_api.api_serializers.recruitment.serializers import (
    CandidateAnswerSerializer,
    CandidateDocumentRequestSerializer,
    CandidateDocumentSerializer,
    CandidateRatingSerializer,
    CandidateSerializer,
    InterviewScheduleSerializer,
    JobOpeningQuestionSerializer,
    LinkedInAccountSerializer,
    RecruitmentSerializer,
    RejectedCandidateSerializer,
    RejectReasonSerializer,
    SkillSerializer,
    SkillZoneCandidateSerializer,
    SkillZoneSerializer,
    StageSerializer,
    SurveyTemplateSerializer,
)
from recruitment.filters import (
    CandidateFilter,
    InterviewFilter,
    LinkedInAccountFilter,
    RecruitmentFilter,
    RejectReasonFilter,
    SkillsFilter,
    SkillZoneCandFilter,
    SkillZoneFilter,
    StageFilter,
    SurveyTemplateFilter,
)
from recruitment.models import (
    Candidate,
    CandidateDocument,
    CandidateDocumentRequest,
    CandidateRating,
    InterviewSchedule,
    LinkedInAccount,
    Recruitment,
    RejectedCandidate,
    RejectReason,
    Skill,
    SkillZone,
    SkillZoneCandidate,
    Stage,
    SurveyTemplate,
)

from ...api_decorators.base.decorators import (
    manager_permission_required,
    permission_required,
)
from ...api_methods.base.methods import groupby_queryset, permission_based_queryset


class RecruitmentAPIView(APIView):
    """
    Base for every recruitment API view: after the token (JWT) login, set the
    same company context the browser gets from CompanyMiddleware. API calls
    authenticate inside the view, after the middleware has run, so without
    this every company-scoped lookup here returned all companies' data.
    """

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        django_request = getattr(request, "_request", request)
        if getattr(django_request, "user", None) is not None and django_request.user.is_authenticated:
            from base.middleware import CompanyMiddleware

            CompanyMiddleware(lambda _request: None)(django_request)


def object_check(cls, pk):
    try:
        obj = cls.objects.get(id=pk)
        return obj
    except cls.DoesNotExist:
        return None


# Recruitment Views
class RecruitmentGetCreateAPIView(RecruitmentAPIView):
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend]
    filterset_class = RecruitmentFilter
    queryset = Recruitment.objects.none()  # For drf-yasg schema generation

    def get_queryset(self, request=None):
        # Handle schema generation for DRF-YASG
        if getattr(self, "swagger_fake_view", False) or request is None:
            return Recruitment.objects.none()
        queryset = Recruitment.objects.all()
        user = request.user
        # checking user level permissions
        perm = "recruitment.view_recruitment"
        queryset = permission_based_queryset(user, perm, queryset, user_obj=True)
        return queryset

    def get(self, request, pk=None):
        if pk:
            recruitment = object_check(Recruitment, pk)
            if recruitment is None:
                return Response({"error": _("Recruitment not found")}, status=404)
            serializer = RecruitmentSerializer(recruitment)
            return Response(serializer.data, status=200)

        recruitments = self.get_queryset(request)
        filterset = self.filterset_class(request.GET, queryset=recruitments)

        # groupby section
        field_name = request.GET.get("groupby_field", None)
        if field_name:
            url = request.build_absolute_uri()
            return groupby_queryset(request, url, field_name, filterset.qs)

        # pagination section
        paginator = PageNumberPagination()
        page = paginator.paginate_queryset(filterset.qs, request)
        serializer = RecruitmentSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)

    @permission_required("recruitment.add_recruitment")
    def post(self, request, **kwargs):
        # serializer = RecruitmentSerializer(data=request.data)
        # if serializer.is_valid():
        #     serializer.save()
        #     return Response(serializer.data, status=status.HTTP_201_CREATED)
        # return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        #
        # Same company rule as the UI form: one of the user's own companies
        # (chosen, or resolved from their login), never another tenant's.
        from recruitment.services import job_opening as lifecycle
        from recruitment.services.authorization import (
            resolve_company_for_new_job_opening,
            selectable_companies_for_user,
        )

        data = request.data.copy() if hasattr(request.data, "copy") else dict(request.data)
        chosen = data.get("company_id_write")
        allowed = selectable_companies_for_user(request.user)
        if chosen:
            if not allowed.filter(pk=chosen).exists():
                return Response(
                    {"error": _("You can't create a job opening for that company.")},
                    status=403,
                )
        else:
            company = resolve_company_for_new_job_opening(request.user)
            if company is None:
                return Response(
                    {"error": _("Choose the company (company_id_write) for this job opening.")},
                    status=400,
                )
            data["company_id_write"] = company.pk
        serializer = RecruitmentSerializer(data=data)
        # Job Position is optional (PRD); DRF's unique-together validator would
        # demand it. The database constraint still prevents real duplicates.
        serializer.validators = []
        if serializer.is_valid():
            job_opening = serializer.save()
            lifecycle.record_created(request.user, job_opening)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class RecruitmentGetUpdateDeleteAPIView(RecruitmentAPIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        recruitment = object_check(Recruitment, pk)
        if recruitment is None:
            return Response({"error": _("Recruitment not found")}, status=404)
        serializer = RecruitmentSerializer(recruitment)
        return Response(serializer.data, status=200)

    @permission_required("recruitment.change_recruitment")
    def put(self, request, pk):
        recruitment = object_check(Recruitment, pk)
        if recruitment is None:
            return Response({"error": _("Recruitment not found")}, status=404)
        # Same rule as the UI edit form: only the opening's managers / HR.
        from recruitment.services.authorization import user_can_manage_job_opening

        if not user_can_manage_job_opening(
            request.user, recruitment, "recruitment.change_recruitment"
        ):
            return Response(
                {"error": _("Only this job opening's managers can edit it.")}, status=403
            )
        if "company_id_write" in request.data or "company_id" in request.data:
            return Response(
                {"error": _("A job opening's company can't be changed.")}, status=400
            )
        # A closed (or removed) opening is read-only: Duplicate it instead.
        if recruitment.status in (
            Recruitment.Status.CLOSED,
            Recruitment.Status.REMOVED,
        ):
            return Response(
                {
                    "error": _(
                        "A closed job opening can't be edited. Use Duplicate to "
                        "post it again."
                    )
                },
                status=400,
            )
        serializer = RecruitmentSerializer(recruitment, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=200)
        return Response(serializer.errors, status=400)

    # Not offered: job openings are taken down with Remove (lifecycle), never hard-deleted.
    # @permission_required("recruitment.delete_recruitment")
    # def delete(self, request, pk):
    #     recruitment = object_check(Recruitment, pk)
    #     if recruitment is None:
    #         return Response({"error": _("Recruitment not found")}, status=404)
    #     try:
    #         recruitment.delete()
    #         return Response(status=status.HTTP_204_NO_CONTENT)
    #     except Exception as e:
    #         return Response({"error": str(e)}, status=400)


class RecruitmentLifecycleAPIView(RecruitmentAPIView):
    """
    Explicit job-opening lifecycle transitions.

    The serializer marks status and its mirrors read-only, so these endpoints
    are the only way an API client can move a job opening through

        DRAFT -> REVIEW -> PUBLISHED -> CLOSED

    Each call delegates to recruitment.services.job_opening, which enforces
    company scope, the object-scoped change_recruitment permission, a valid
    transition, row locking and the business audit event -- identically to the
    UI, because both go through the same service.

    POST /api/recruitment/recruitment/<pk>/<action>/
        action in: submit-for-review | send-back | publish | close | remove
    """

    permission_classes = [IsAuthenticated]

    ACTIONS = {
        "submit-for-review": "submit_for_review",
        "send-back": "send_back_for_changes",
        "publish": "publish",
        "close": "close",
        "remove": "remove",
    }

    def post(self, request, pk, action):
        from recruitment.services import job_opening as lifecycle
        from recruitment.services.errors import (
            JobOpeningNotFound,
            PublicationValidationError,
            RecruitmentError,
            RecruitmentPermissionDenied,
        )

        operation_name = self.ACTIONS.get(action)
        if operation_name is None:
            return Response(
                {"error": _("Unknown lifecycle action."), "code": "unknown_action"},
                status=400,
            )
        operation = getattr(lifecycle, operation_name)

        kwargs = {}
        remark = (request.data.get("remark") or "").strip()
        reason = (request.data.get("reason") or "").strip()
        if action == "send-back" and remark:
            kwargs["remark"] = remark
        if action in ("close", "remove") and reason:
            kwargs["reason"] = reason

        try:
            job_opening = operation(request.user, pk, **kwargs)
        except PublicationValidationError as error:
            # Every blocker at once, so a client is not forced to fix them
            # one round-trip at a time.
            return Response(
                {
                    "error": str(error),
                    "code": error.code,
                    "blockers": [str(blocker) for blocker in error.blockers],
                },
                status=400,
            )
        except JobOpeningNotFound as error:
            return Response({"error": str(error), "code": error.code}, status=404)
        except RecruitmentPermissionDenied as error:
            return Response({"error": str(error), "code": error.code}, status=403)
        except RecruitmentError as error:
            # Invalid transition, already-removed, not-accepting-candidates:
            # a business rule refused, not a server fault.
            return Response({"error": str(error), "code": error.code}, status=400)

        return Response(RecruitmentSerializer(job_opening).data, status=200)


# Stage Views
class StageGetCreateAPIView(RecruitmentAPIView):
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend]
    filterset_class = StageFilter
    queryset = Stage.objects.none()  # For drf-yasg schema generation

    def get_queryset(self, request=None, recruitment_id=None):
        # Handle schema generation for DRF-YASG
        if getattr(self, "swagger_fake_view", False) or request is None:
            return Stage.objects.none()
        queryset = Stage.objects.all()
        if recruitment_id:
            queryset = queryset.filter(recruitment_id=recruitment_id)
        user = request.user
        # checking user level permissions
        perm = "recruitment.view_stage"
        queryset = permission_based_queryset(user, perm, queryset, user_obj=True)
        return queryset

    def get(self, request, pk=None, recruitment_id=None):
        if pk:
            stage = object_check(Stage, pk)
            if stage is None:
                return Response({"error": _("Stage not found")}, status=404)
            serializer = StageSerializer(stage)
            return Response(serializer.data, status=200)

        stages = self.get_queryset(request, recruitment_id)
        filterset = self.filterset_class(request.GET, queryset=stages)

        # groupby section
        field_name = request.GET.get("groupby_field", None)
        if field_name:
            url = request.build_absolute_uri()
            return groupby_queryset(request, url, field_name, filterset.qs)

        # pagination section
        paginator = PageNumberPagination()
        page = paginator.paginate_queryset(filterset.qs, request)
        serializer = StageSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)

    @permission_required("recruitment.add_stage")
    def post(self, request, recruitment_id=None, **kwargs):
        from recruitment.services.authorization import is_drive_manager

        _rec_id = recruitment_id or request.data.get("recruitment_id_write")
        _opening = Recruitment.objects.filter(pk=_rec_id).first() if _rec_id else None
        if _opening is not None and not is_drive_manager(request.user, _opening):
            return Response(
                {"error": _("Only this job opening's managers can add stages.")}, status=403
            )
        if request.data.get("stage_type") in ("applied", "final_hr_round", "hired", "cancelled"):
            return Response(
                {"error": _("Fixed stages are created automatically; add a custom stage.")},
                status=400,
            )
        data = request.data.copy()
        if (
            recruitment_id
            and not data.get("recruitment_id_write")
            and not data.get("recruitment_id")
        ):
            data["recruitment_id_write"] = recruitment_id
        serializer = StageSerializer(data=data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class StageGetUpdateDeleteAPIView(RecruitmentAPIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        stage = object_check(Stage, pk)
        if stage is None:
            return Response({"error": _("Stage not found")}, status=404)
        serializer = StageSerializer(stage)
        return Response(serializer.data, status=200)

    @permission_required("recruitment.change_stage")
    def put(self, request, pk):
        from recruitment.services.authorization import is_drive_manager

        _stage = Stage.objects.filter(pk=pk).first()
        if _stage is not None and not is_drive_manager(request.user, _stage.recruitment_id):
            return Response(
                {"error": _("Only this job opening's managers can edit stages.")}, status=403
            )
        if "stage_managers_ids" in request.data and not [
            m for m in (request.data.getlist("stage_managers_ids") if hasattr(request.data, "getlist") else request.data.get("stage_managers_ids") or []) if m
        ]:
            return Response(
                {"error": _("Every stage needs at least one Stage Manager.")}, status=400
            )
        stage = object_check(Stage, pk)
        if stage is None:
            return Response({"error": _("Stage not found")}, status=404)
        serializer = StageSerializer(stage, data=request.data, partial=True)
        if serializer.is_valid():
            from django.core.exceptions import ValidationError as _ModelValidationError

            try:
                serializer.save()
            except _ModelValidationError as error:
                return Response({"error": " ".join(error.messages)}, status=400)
            return Response(serializer.data, status=200)
        return Response(serializer.errors, status=400)

    @permission_required("recruitment.delete_stage")
    def delete(self, request, pk):
        from recruitment.services.authorization import is_drive_manager

        _stage = Stage.objects.filter(pk=pk).first()
        if _stage is not None and _stage.is_fixed:
            return Response(
                {"error": _("Applied, Final HR Round and Hired are fixed stages and cannot be removed.")},
                status=400,
            )
        if _stage is not None and not is_drive_manager(request.user, _stage.recruitment_id):
            return Response(
                {"error": _("Only this job opening's managers can remove stages.")}, status=403
            )
        stage = object_check(Stage, pk)
        if stage is None:
            return Response({"error": _("Stage not found")}, status=404)
        try:
            stage.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)
        except Exception as e:
            return Response({"error": str(e)}, status=400)


# Candidate Views
class CandidateGetCreateAPIView(RecruitmentAPIView):
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend]
    filterset_class = CandidateFilter
    queryset = Candidate.objects.none()  # For drf-yasg schema generation

    def get_queryset(self, request=None, recruitment_id=None, stage_id=None):
        # Handle schema generation for DRF-YASG
        if getattr(self, "swagger_fake_view", False) or request is None:
            return Candidate.objects.none()

        # Company-scoped at the database. This previously started from
        # Candidate.objects.all() and relied on permission_based_queryset,
        # which returns the FULL queryset to anyone holding view_candidate --
        # i.e. every tenant's candidates -- and otherwise filters on an
        # employee_id field that Candidate does not have.
        from recruitment.services import candidate as candidate_service

        queryset = candidate_service.accessible_candidates(request.user)
        if recruitment_id:
            queryset = queryset.filter(recruitment_id=recruitment_id)
        if stage_id:
            queryset = queryset.filter(stage_id=stage_id)
        return queryset

    def get(self, request, pk=None, recruitment_id=None, stage_id=None):
        if pk:
            # Scoped lookup: another tenant's id answers 404, never 200.
            from recruitment.services import candidate as candidate_service
            from recruitment.services.errors import (
                CandidateNotFound,
                RecruitmentPermissionDenied,
            )

            try:
                candidate = candidate_service.get_candidate_for_user(request.user, pk)
            except CandidateNotFound:
                return Response({"error": _("Candidate not found")}, status=404)
            except RecruitmentPermissionDenied as error:
                return Response({"error": str(error)}, status=403)
            serializer = CandidateSerializer(candidate)
            return Response(serializer.data, status=200)

        candidates = self.get_queryset(request, recruitment_id, stage_id)
        filterset = self.filterset_class(request.GET, queryset=candidates)

        # groupby section
        field_name = request.GET.get("groupby_field", None)
        if field_name:
            url = request.build_absolute_uri()
            return groupby_queryset(request, url, field_name, filterset.qs)

        # pagination section
        paginator = PageNumberPagination()
        page = paginator.paginate_queryset(filterset.qs, request)
        serializer = CandidateSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)

    @permission_required("recruitment.add_candidate")
    def post(self, request, recruitment_id=None, stage_id=None, **kwargs):
        # data = request.data.copy()
        # if (
        #     recruitment_id
        #     and not data.get("recruitment_id_write")
        #     and not data.get("recruitment_id")
        # ):
        #     data["recruitment_id_write"] = recruitment_id
        # if stage_id and not data.get("stage_id_write") and not data.get("stage_id"):
        #     data["stage_id_write"] = stage_id
        # serializer = CandidateSerializer(data=data)
        # if serializer.is_valid():
        #     serializer.save()
        #     return Response(serializer.data, status=status.HTTP_201_CREATED)
        # return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        #
        # Manual candidate creation (PRD Form 3), with the app's rules: a
        # Published opening the user manages, any stage up to Final HR Round
        # (never Hired or Rejected), created through the candidate service.
        from recruitment.services import candidate as candidate_service
        from recruitment.services.authorization import (
            has_company_wide_job_opening_authority,
            manages_job_opening,
        )
        from recruitment.services.errors import RecruitmentError

        data = request.data.copy() if hasattr(request.data, "copy") else dict(request.data)
        opening_id = recruitment_id or data.get("recruitment_id_write") or data.get("recruitment_id")
        stage_ref = stage_id or data.get("stage_id_write") or data.get("stage_id")
        for key in PROTECTED_CANDIDATE_FIELDS:
            data.pop(key, None)

        stage = None
        if stage_ref:
            stage = Stage.objects.filter(pk=stage_ref).select_related("recruitment_id").first()
            if stage is None:
                return Response({"error": _("Stage not found")}, status=404)
            opening_id = opening_id or stage.recruitment_id_id
        opening = None
        if opening_id:
            opening = Recruitment.objects.filter(pk=opening_id).first()
            if opening is None:
                return Response({"error": _("Job opening not found")}, status=404)
            if opening.status != Recruitment.Status.PUBLISHED:
                return Response(
                    {"error": _("Only a published job opening can receive candidates.")},
                    status=400,
                )
            # Company-scoped: an opening in another company is refused even for
            # HR with company-wide authority (JWT calls have no company context).
            from recruitment.services.authorization import is_drive_manager

            if not is_drive_manager(request.user, opening):
                return Response(
                    {"error": _("Only this job opening's managers can add candidates.")},
                    status=403,
                )
            if stage is None:
                stage = candidate_service.entry_stage(opening)
            elif stage.recruitment_id_id != opening.pk:
                return Response(
                    {"error": _("The stage does not belong to this job opening.")},
                    status=400,
                )
            if stage is not None and stage.stage_type in ("hired", "cancelled"):
                return Response(
                    {"error": _("A candidate cannot be added straight into Hired or Rejected.")},
                    status=400,
                )

        serializer = CandidateSerializer(data=data)
        # Opening and stage were resolved above, not taken from the body, so the
        # (email, opening) unique-together validator -- which would demand the
        # opening field -- is replaced by the duplicate check below.
        serializer.validators = []
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        fields = {
            k: v for k, v in serializer.validated_data.items()
            if k not in PROTECTED_CANDIDATE_FIELDS
        }
        position = fields.get("job_position_id")
        if opening and position and not opening.open_positions.filter(pk=position.pk).exists():
            return Response(
                {"error": _("The job position is not part of this job opening.")},
                status=400,
            )
        if opening and candidate_service.existing_candidate_with_email(
            fields.get("email"), job_opening=opening
        ):
            return Response(
                {"error": _("This email has already applied to this job opening.")},
                status=400,
            )
        try:
            candidate = candidate_service.create_candidate(
                request.user, job_opening=opening, stage_id=stage, **fields
            )
        except RecruitmentError as error:
            return Response({"error": str(error), "code": error.code}, status=400)
        return Response(CandidateSerializer(candidate).data, status=status.HTTP_201_CREATED)


#: Candidate fields an API client may not set: the opening, stage and outcome
#: change only through the pipeline actions (Move Forward / Reject / handoff),
#: the handoff data only on the Final HR Round -> Hired form, and the company is
#: derived server-side.
PROTECTED_CANDIDATE_FIELDS = {
    "recruitment_id", "recruitment_id_write",
    "stage_id", "stage_id_write",
    "hired", "hired_date", "canceled",
    "converted", "converted_employee_id", "converted_employee_id_write",
    "start_onboard", "joining_date", "offered_ctc",
    "handoff_budget_min", "handoff_budget_max",
    "company_id", "company_id_write", "is_active",
}


def _protected_fields_in(data):
    return sorted(k for k in data.keys() if k in PROTECTED_CANDIDATE_FIELDS)


class CandidateGetUpdateDeleteAPIView(RecruitmentAPIView):
    """
    Candidate detail.

    Every method resolves the candidate through the company-scoped service
    lookup rather than object_check(), which used an unscoped
    Candidate.objects.get() -- so any authenticated user could read, modify or
    delete another company's candidate by guessing an id.
    """

    permission_classes = [IsAuthenticated]

    def _resolve(self, request, pk, permission):
        from recruitment.services import candidate as candidate_service
        from recruitment.services.errors import (
            CandidateNotFound,
            RecruitmentPermissionDenied,
        )

        try:
            return (
                candidate_service.get_candidate_for_user(request.user, pk, permission),
                None,
            )
        except CandidateNotFound:
            return None, Response({"error": _("Candidate not found")}, status=404)
        except RecruitmentPermissionDenied as error:
            return None, Response({"error": str(error)}, status=403)

    def get(self, request, pk):
        candidate, error = self._resolve(request, pk, "recruitment.view_candidate")
        if error:
            return error
        serializer = CandidateSerializer(candidate)
        return Response(serializer.data, status=200)

    # Not offered: the UI has no candidate edit (PRD: editing basic details is a future iteration).
    # @permission_required("recruitment.change_candidate")
    # def put(self, request, pk):
    #     candidate, error = self._resolve(request, pk, "recruitment.change_candidate")
    #     if error:
    #         return error
    #     data = request.data.copy() if hasattr(request.data, "copy") else dict(request.data)
    #     # Company is derived server-side from the job opening / acting user and
    #     # is never accepted from the client.
    #     data.pop("company_id", None)
    #     data.pop("company_id_write", None)
    #     # Opening, stage, outcome and handoff data change only through the
    #     # pipeline actions, never by editing the record.
    #     blocked = _protected_fields_in(data)
    #     if blocked:
    #         return Response(
    #             {
    #                 "error": _(
    #                     "These fields cannot be changed here; use the pipeline "
    #                     "actions (Move Forward, Reject, hiring handoff): %(fields)s"
    #                 )
    #                 % {"fields": ", ".join(blocked)},
    #                 "code": "protected_fields",
    #             },
    #             status=400,
    #         )
    #     serializer = CandidateSerializer(candidate, data=data, partial=True)
    #     if serializer.is_valid():
    #         serializer.save()
    #         return Response(serializer.data, status=200)
    #     return Response(serializer.errors, status=400)

    # Removed: candidates are never deleted (PRD: nothing leaves the Candidate
    # Pool), so the API offers no DELETE; DRF answers 405 Method Not Allowed.
    # @permission_required("recruitment.delete_candidate")
    # def delete(self, request, pk):
    #     candidate, error = self._resolve(request, pk, "recruitment.delete_candidate")
    #     if error:
    #         return error
    #     try:
    #         candidate.delete()
    #         return Response(status=status.HTTP_204_NO_CONTENT)
    #     except Exception as e:
    #         return Response({"error": str(e)}, status=400)


# Interview Schedule Views
class InterviewScheduleGetCreateAPIView(RecruitmentAPIView):
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend]
    filterset_class = InterviewFilter
    queryset = InterviewSchedule.objects.none()  # For drf-yasg schema generation

    def get_queryset(self, request=None, candidate_id=None):
        # Handle schema generation for DRF-YASG
        if getattr(self, "swagger_fake_view", False) or request is None:
            return InterviewSchedule.objects.none()
        queryset = InterviewSchedule.objects.all()
        if candidate_id:
            queryset = queryset.filter(candidate_id=candidate_id)
        user = request.user
        # checking user level permissions
        perm = "recruitment.view_interviewschedule"
        queryset = permission_based_queryset(user, perm, queryset, user_obj=True)
        return queryset

    def get(self, request, pk=None, candidate_id=None):
        if pk:
            interview = object_check(InterviewSchedule, pk)
            if interview is None:
                return Response({"error": _("InterviewSchedule not found")}, status=404)
            serializer = InterviewScheduleSerializer(interview)
            return Response(serializer.data, status=200)

        interviews = self.get_queryset(request, candidate_id)
        filterset = self.filterset_class(request.GET, queryset=interviews)

        # groupby section
        field_name = request.GET.get("groupby_field", None)
        if field_name:
            url = request.build_absolute_uri()
            return groupby_queryset(request, url, field_name, filterset.qs)

        # pagination section
        paginator = PageNumberPagination()
        page = paginator.paginate_queryset(filterset.qs, request)
        serializer = InterviewScheduleSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)

    @permission_required("recruitment.add_interviewschedule")
    def post(self, request, candidate_id=None, **kwargs):
        data = request.data.copy()
        if (
            candidate_id
            and not data.get("candidate_id_write")
            and not data.get("candidate_id")
        ):
            data["candidate_id_write"] = candidate_id
        serializer = InterviewScheduleSerializer(data=data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class InterviewScheduleGetUpdateDeleteAPIView(RecruitmentAPIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        interview = object_check(InterviewSchedule, pk)
        if interview is None:
            return Response({"error": _("InterviewSchedule not found")}, status=404)
        serializer = InterviewScheduleSerializer(interview)
        return Response(serializer.data, status=200)

    @permission_required("recruitment.change_interviewschedule")
    def put(self, request, pk):
        interview = object_check(InterviewSchedule, pk)
        if interview is None:
            return Response({"error": _("InterviewSchedule not found")}, status=404)
        serializer = InterviewScheduleSerializer(
            interview, data=request.data, partial=True
        )
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=200)
        return Response(serializer.errors, status=400)

    @permission_required("recruitment.delete_interviewschedule")
    def delete(self, request, pk):
        interview = object_check(InterviewSchedule, pk)
        if interview is None:
            return Response({"error": _("InterviewSchedule not found")}, status=404)
        try:
            interview.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)
        except Exception as e:
            return Response({"error": str(e)}, status=400)


# Skill Views
class SkillGetCreateAPIView(RecruitmentAPIView):
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend]
    filterset_class = SkillsFilter
    queryset = Skill.objects.none()  # For drf-yasg schema generation

    def get_queryset(self, request=None):
        # Handle schema generation for DRF-YASG
        if getattr(self, "swagger_fake_view", False) or request is None:
            return Skill.objects.none()
        return Skill.objects.all()

    def get(self, request, pk=None):
        if pk:
            skill = object_check(Skill, pk)
            if skill is None:
                return Response({"error": _("Skill not found")}, status=404)
            serializer = SkillSerializer(skill)
            return Response(serializer.data, status=200)

        skills = self.get_queryset(request)
        filterset = self.filterset_class(request.GET, queryset=skills)

        # pagination section
        paginator = PageNumberPagination()
        page = paginator.paginate_queryset(filterset.qs, request)
        serializer = SkillSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)

    @permission_required("recruitment.add_skill")
    def post(self, request, **kwargs):
        serializer = SkillSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class SkillGetUpdateDeleteAPIView(RecruitmentAPIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        skill = object_check(Skill, pk)
        if skill is None:
            return Response({"error": _("Skill not found")}, status=404)
        serializer = SkillSerializer(skill)
        return Response(serializer.data, status=200)

    @permission_required("recruitment.change_skill")
    def put(self, request, pk):
        skill = object_check(Skill, pk)
        if skill is None:
            return Response({"error": _("Skill not found")}, status=404)
        serializer = SkillSerializer(skill, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=200)
        return Response(serializer.errors, status=400)

    @permission_required("recruitment.delete_skill")
    def delete(self, request, pk):
        skill = object_check(Skill, pk)
        if skill is None:
            return Response({"error": _("Skill not found")}, status=404)
        try:
            skill.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)
        except Exception as e:
            return Response({"error": str(e)}, status=400)


# Survey Template Views
class SurveyTemplateGetCreateAPIView(RecruitmentAPIView):
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend]
    filterset_class = SurveyTemplateFilter
    queryset = SurveyTemplate.objects.none()  # For drf-yasg schema generation

    def get_queryset(self, request=None):
        # Handle schema generation for DRF-YASG
        if getattr(self, "swagger_fake_view", False) or request is None:
            return SurveyTemplate.objects.none()
        queryset = SurveyTemplate.objects.all()
        user = request.user
        # checking user level permissions
        perm = "recruitment.view_surveytemplate"
        queryset = permission_based_queryset(user, perm, queryset, user_obj=True)
        return queryset

    def get(self, request, pk=None):
        if pk:
            template = object_check(SurveyTemplate, pk)
            if template is None:
                return Response({"error": _("SurveyTemplate not found")}, status=404)
            serializer = SurveyTemplateSerializer(template)
            return Response(serializer.data, status=200)

        templates = self.get_queryset(request)
        filterset = self.filterset_class(request.GET, queryset=templates)

        # pagination section
        paginator = PageNumberPagination()
        page = paginator.paginate_queryset(filterset.qs, request)
        serializer = SurveyTemplateSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)

    @permission_required("recruitment.add_surveytemplate")
    def post(self, request, **kwargs):
        from recruitment.services.authorization import selectable_companies_for_user

        chosen = request.data.get("company_id_write")
        if chosen and not selectable_companies_for_user(request.user).filter(pk=chosen).exists():
            return Response(
                {"error": _("You can't create a template for that company.")}, status=403
            )
        serializer = SurveyTemplateSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class SurveyTemplateGetUpdateDeleteAPIView(RecruitmentAPIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        template = object_check(SurveyTemplate, pk)
        if template is None:
            return Response({"error": _("SurveyTemplate not found")}, status=404)
        serializer = SurveyTemplateSerializer(template)
        return Response(serializer.data, status=200)

    @permission_required("recruitment.change_surveytemplate")
    def put(self, request, pk):
        template = object_check(SurveyTemplate, pk)
        if template is None:
            return Response({"error": _("SurveyTemplate not found")}, status=404)
        if "company_id_write" in request.data or "company_id" in request.data:
            return Response(
                {"error": _("A template's company can't be changed.")}, status=400
            )
        serializer = SurveyTemplateSerializer(template, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=200)
        return Response(serializer.errors, status=400)

    @permission_required("recruitment.delete_surveytemplate")
    def delete(self, request, pk):
        template = object_check(SurveyTemplate, pk)
        if template is None:
            return Response({"error": _("SurveyTemplate not found")}, status=404)
        # Same rule as the UI: a template any job opening uses can't be deleted.
        openings = list(
            Recruitment._base_manager.filter(survey_templates=template)
            .order_by("title")
            .values_list("title", flat=True)
            .distinct()[:4]
        )
        if openings:
            shown = ", ".join(openings[:3]) + (" and others" if len(openings) > 3 else "")
            return Response(
                {
                    "error": _(
                        "This template can't be deleted: it is used by job opening(s) "
                        "%(openings)s. Remove it from those openings first."
                    )
                    % {"openings": shown}
                },
                status=400,
            )
        try:
            template.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)
        except Exception as e:
            return Response({"error": str(e)}, status=400)


# Skill Zone Views
class SkillZoneGetCreateAPIView(RecruitmentAPIView):
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend]
    filterset_class = SkillZoneFilter
    queryset = SkillZone.objects.none()  # For drf-yasg schema generation

    def get_queryset(self, request=None):
        # Handle schema generation for DRF-YASG
        if getattr(self, "swagger_fake_view", False) or request is None:
            return SkillZone.objects.none()
        queryset = SkillZone.objects.all()
        user = request.user
        # checking user level permissions
        perm = "recruitment.view_skillzone"
        # queryset = permission_based_queryset(user, perm, queryset, user_obj=True)
        # No employee_id on this model, so the helper's manager branch crashed
        # (500) for reporting managers without the permission.
        if not user.has_perm(perm):
            return queryset.none()
        return queryset

    def get(self, request, pk=None):
        if pk:
            skill_zone = object_check(SkillZone, pk)
            if skill_zone is None:
                return Response({"error": _("SkillZone not found")}, status=404)
            serializer = SkillZoneSerializer(skill_zone)
            return Response(serializer.data, status=200)

        skill_zones = self.get_queryset(request)
        filterset = self.filterset_class(request.GET, queryset=skill_zones)

        # pagination section
        paginator = PageNumberPagination()
        page = paginator.paginate_queryset(filterset.qs, request)
        serializer = SkillZoneSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)

    @permission_required("recruitment.add_skillzone")
    def post(self, request, **kwargs):
        serializer = SkillZoneSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class SkillZoneGetUpdateDeleteAPIView(RecruitmentAPIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        skill_zone = object_check(SkillZone, pk)
        if skill_zone is None:
            return Response({"error": _("SkillZone not found")}, status=404)
        serializer = SkillZoneSerializer(skill_zone)
        return Response(serializer.data, status=200)

    @permission_required("recruitment.change_skillzone")
    def put(self, request, pk):
        skill_zone = object_check(SkillZone, pk)
        if skill_zone is None:
            return Response({"error": _("SkillZone not found")}, status=404)
        serializer = SkillZoneSerializer(skill_zone, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=200)
        return Response(serializer.errors, status=400)

    @permission_required("recruitment.delete_skillzone")
    def delete(self, request, pk):
        skill_zone = object_check(SkillZone, pk)
        if skill_zone is None:
            return Response({"error": _("SkillZone not found")}, status=404)
        try:
            skill_zone.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)
        except Exception as e:
            return Response({"error": str(e)}, status=400)


# Skill Zone Candidate Views
class SkillZoneCandidateGetCreateAPIView(RecruitmentAPIView):
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend]
    filterset_class = SkillZoneCandFilter
    queryset = SkillZoneCandidate.objects.none()  # For drf-yasg schema generation

    def get_queryset(self, request=None, candidate_id=None, skill_zone_id=None):
        # Handle schema generation for DRF-YASG
        if getattr(self, "swagger_fake_view", False) or request is None:
            return SkillZoneCandidate.objects.none()
        queryset = SkillZoneCandidate.objects.all()
        if candidate_id:
            queryset = queryset.filter(candidate_id=candidate_id)
        if skill_zone_id:
            queryset = queryset.filter(skill_zone_id=skill_zone_id)
        user = request.user
        # checking user level permissions
        perm = "recruitment.view_skillzonecandidate"
        # queryset = permission_based_queryset(user, perm, queryset, user_obj=True)
        # No employee_id on this model, so the helper's manager branch crashed
        # (500) for reporting managers without the permission.
        if not user.has_perm(perm):
            return queryset.none()
        return queryset

    def get(self, request, pk=None, candidate_id=None, skill_zone_id=None):
        if pk:
            skill_zone_candidate = object_check(SkillZoneCandidate, pk)
            if skill_zone_candidate is None:
                return Response(
                    {"error": _("SkillZoneCandidate not found")}, status=404
                )
            serializer = SkillZoneCandidateSerializer(skill_zone_candidate)
            return Response(serializer.data, status=200)

        skill_zone_candidates = self.get_queryset(request, candidate_id, skill_zone_id)
        filterset = self.filterset_class(request.GET, queryset=skill_zone_candidates)

        # groupby section
        field_name = request.GET.get("groupby_field", None)
        if field_name:
            url = request.build_absolute_uri()
            return groupby_queryset(request, url, field_name, filterset.qs)

        # pagination section
        paginator = PageNumberPagination()
        page = paginator.paginate_queryset(filterset.qs, request)
        serializer = SkillZoneCandidateSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)

    @permission_required("recruitment.add_skillzonecandidate")
    def post(self, request, candidate_id=None, skill_zone_id=None, **kwargs):
        data = request.data.copy()
        if (
            candidate_id
            and not data.get("candidate_id_write")
            and not data.get("candidate_id")
        ):
            data["candidate_id_write"] = candidate_id
        if (
            skill_zone_id
            and not data.get("skill_zone_id_write")
            and not data.get("skill_zone_id")
        ):
            data["skill_zone_id_write"] = skill_zone_id
        serializer = SkillZoneCandidateSerializer(data=data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class SkillZoneCandidateGetUpdateDeleteAPIView(RecruitmentAPIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        skill_zone_candidate = object_check(SkillZoneCandidate, pk)
        if skill_zone_candidate is None:
            return Response({"error": _("SkillZoneCandidate not found")}, status=404)
        serializer = SkillZoneCandidateSerializer(skill_zone_candidate)
        return Response(serializer.data, status=200)

    @permission_required("recruitment.change_skillzonecandidate")
    def put(self, request, pk):
        skill_zone_candidate = object_check(SkillZoneCandidate, pk)
        if skill_zone_candidate is None:
            return Response({"error": _("SkillZoneCandidate not found")}, status=404)
        serializer = SkillZoneCandidateSerializer(
            skill_zone_candidate, data=request.data, partial=True
        )
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=200)
        return Response(serializer.errors, status=400)

    @permission_required("recruitment.delete_skillzonecandidate")
    def delete(self, request, pk):
        skill_zone_candidate = object_check(SkillZoneCandidate, pk)
        if skill_zone_candidate is None:
            return Response({"error": _("SkillZoneCandidate not found")}, status=404)
        try:
            skill_zone_candidate.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)
        except Exception as e:
            return Response({"error": str(e)}, status=400)


# Candidate Rating Views
class CandidateRatingGetCreateAPIView(RecruitmentAPIView):
    permission_classes = [IsAuthenticated]
    queryset = CandidateRating.objects.none()  # For drf-yasg schema generation

    def get_queryset(self, request=None, candidate_id=None):
        # Handle schema generation for DRF-YASG
        if getattr(self, "swagger_fake_view", False) or request is None:
            return CandidateRating.objects.none()
        queryset = CandidateRating.objects.all()
        if candidate_id:
            queryset = queryset.filter(candidate_id=candidate_id)
        user = request.user
        # checking user level permissions
        perm = "recruitment.view_candidaterating"
        queryset = permission_based_queryset(user, perm, queryset, user_obj=True)
        return queryset

    def get(self, request, pk=None, candidate_id=None):
        if pk:
            rating = object_check(CandidateRating, pk)
            if rating is None:
                return Response({"error": _("CandidateRating not found")}, status=404)
            serializer = CandidateRatingSerializer(rating)
            return Response(serializer.data, status=200)

        ratings = self.get_queryset(request, candidate_id)
        paginator = PageNumberPagination()
        page = paginator.paginate_queryset(ratings, request)
        serializer = CandidateRatingSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)

    @permission_required("recruitment.add_candidaterating")
    def post(self, request, candidate_id=None, **kwargs):
        data = request.data.copy()
        if (
            candidate_id
            and not data.get("candidate_id_write")
            and not data.get("candidate_id")
        ):
            data["candidate_id_write"] = candidate_id
        serializer = CandidateRatingSerializer(data=data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class CandidateRatingGetUpdateDeleteAPIView(RecruitmentAPIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        rating = object_check(CandidateRating, pk)
        if rating is None:
            return Response({"error": _("CandidateRating not found")}, status=404)
        serializer = CandidateRatingSerializer(rating)
        return Response(serializer.data, status=200)

    @permission_required("recruitment.change_candidaterating")
    def put(self, request, pk):
        rating = object_check(CandidateRating, pk)
        if rating is None:
            return Response({"error": _("CandidateRating not found")}, status=404)
        serializer = CandidateRatingSerializer(rating, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=200)
        return Response(serializer.errors, status=400)

    @permission_required("recruitment.delete_candidaterating")
    def delete(self, request, pk):
        rating = object_check(CandidateRating, pk)
        if rating is None:
            return Response({"error": _("CandidateRating not found")}, status=404)
        try:
            rating.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)
        except Exception as e:
            return Response({"error": str(e)}, status=400)


# Reject Reason Views
class RejectReasonGetCreateAPIView(RecruitmentAPIView):
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend]
    filterset_class = RejectReasonFilter
    queryset = RejectReason.objects.none()  # For drf-yasg schema generation

    def get_queryset(self, request=None):
        # Handle schema generation for DRF-YASG
        if getattr(self, "swagger_fake_view", False) or request is None:
            return RejectReason.objects.none()
        queryset = RejectReason.objects.all()
        user = request.user
        # checking user level permissions
        perm = "recruitment.view_rejectreason"
        # queryset = permission_based_queryset(user, perm, queryset, user_obj=True)
        # Reject reasons have no employee_id, so the helper's manager branch
        # crashed (500) for reporting managers without the permission.
        if not user.has_perm(perm):
            return queryset.none()
        return queryset

    def get(self, request, pk=None):
        if pk:
            reason = object_check(RejectReason, pk)
            if reason is None:
                return Response({"error": _("RejectReason not found")}, status=404)
            serializer = RejectReasonSerializer(reason)
            return Response(serializer.data, status=200)

        reasons = self.get_queryset(request)
        filterset = self.filterset_class(request.GET, queryset=reasons)

        # pagination section
        paginator = PageNumberPagination()
        page = paginator.paginate_queryset(filterset.qs, request)
        serializer = RejectReasonSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)

    @permission_required("recruitment.add_rejectreason")
    def post(self, request, **kwargs):
        serializer = RejectReasonSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class RejectReasonGetUpdateDeleteAPIView(RecruitmentAPIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        reason = object_check(RejectReason, pk)
        if reason is None:
            return Response({"error": _("RejectReason not found")}, status=404)
        serializer = RejectReasonSerializer(reason)
        return Response(serializer.data, status=200)

    @permission_required("recruitment.change_rejectreason")
    def put(self, request, pk):
        reason = object_check(RejectReason, pk)
        if reason is None:
            return Response({"error": _("RejectReason not found")}, status=404)
        serializer = RejectReasonSerializer(reason, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=200)
        return Response(serializer.errors, status=400)

    @permission_required("recruitment.delete_rejectreason")
    def delete(self, request, pk):
        reason = object_check(RejectReason, pk)
        if reason is None:
            return Response({"error": _("RejectReason not found")}, status=404)
        try:
            reason.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)
        except Exception as e:
            return Response({"error": str(e)}, status=400)


# Rejected Candidate Views
class RejectedCandidateGetCreateAPIView(RecruitmentAPIView):
    permission_classes = [IsAuthenticated]
    queryset = RejectedCandidate.objects.none()  # For drf-yasg schema generation

    def get_queryset(self, request=None, candidate_id=None):
        # Handle schema generation for DRF-YASG
        if getattr(self, "swagger_fake_view", False) or request is None:
            return RejectedCandidate.objects.none()
        queryset = RejectedCandidate.objects.all()
        if candidate_id:
            queryset = queryset.filter(candidate_id=candidate_id)
        user = request.user
        # checking user level permissions
        perm = "recruitment.view_rejectedcandidate"
        # queryset = permission_based_queryset(user, perm, queryset, user_obj=True)
        # No employee_id on this model, so the helper's manager branch crashed
        # (500) for reporting managers without the permission.
        if not user.has_perm(perm):
            return queryset.none()
        return queryset

    def get(self, request, pk=None, candidate_id=None):
        if pk:
            rejected = object_check(RejectedCandidate, pk)
            if rejected is None:
                return Response({"error": _("RejectedCandidate not found")}, status=404)
            serializer = RejectedCandidateSerializer(rejected)
            return Response(serializer.data, status=200)

        rejected_candidates = self.get_queryset(request, candidate_id)
        paginator = PageNumberPagination()
        page = paginator.paginate_queryset(rejected_candidates, request)
        serializer = RejectedCandidateSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)

    # Not offered: in the UI a rejection happens only through Reject (remark + email).
    # @permission_required("recruitment.add_rejectedcandidate")
    # def post(self, request, candidate_id=None, **kwargs):
    #     data = request.data.copy()
    #     if (
    #         candidate_id
    #         and not data.get("candidate_id_write")
    #         and not data.get("candidate_id")
    #     ):
    #         data["candidate_id_write"] = candidate_id
    #     serializer = RejectedCandidateSerializer(data=data)
    #     if serializer.is_valid():
    #         serializer.save()
    #         return Response(serializer.data, status=status.HTTP_201_CREATED)
    #     return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class RejectedCandidateGetUpdateDeleteAPIView(RecruitmentAPIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        rejected = object_check(RejectedCandidate, pk)
        if rejected is None:
            return Response({"error": _("RejectedCandidate not found")}, status=404)
        serializer = RejectedCandidateSerializer(rejected)
        return Response(serializer.data, status=200)

    # Not offered: in the UI a rejection happens only through Reject (remark + email).
    # @permission_required("recruitment.change_rejectedcandidate")
    # def put(self, request, pk):
    #     rejected = object_check(RejectedCandidate, pk)
    #     if rejected is None:
    #         return Response({"error": _("RejectedCandidate not found")}, status=404)
    #     serializer = RejectedCandidateSerializer(
    #         rejected, data=request.data, partial=True
    #     )
    #     if serializer.is_valid():
    #         serializer.save()
    #         return Response(serializer.data, status=200)
    #     return Response(serializer.errors, status=400)

    # Not offered: in the UI a rejection happens only through Reject (remark + email).
    # @permission_required("recruitment.delete_rejectedcandidate")
    # def delete(self, request, pk):
    #     rejected = object_check(RejectedCandidate, pk)
    #     if rejected is None:
    #         return Response({"error": _("RejectedCandidate not found")}, status=404)
    #     try:
    #         rejected.delete()
    #         return Response(status=status.HTTP_204_NO_CONTENT)
    #     except Exception as e:
    #         return Response({"error": str(e)}, status=400)
    #
    #
    # ndidate Document Request Views


class CandidateDocumentRequestGetCreateAPIView(RecruitmentAPIView):
    permission_classes = [IsAuthenticated]
    queryset = CandidateDocumentRequest.objects.none()  # For drf-yasg schema generation

    def get_queryset(self, request=None, candidate_id=None):
        # Handle schema generation for DRF-YASG
        if getattr(self, "swagger_fake_view", False) or request is None:
            return CandidateDocumentRequest.objects.none()
        queryset = CandidateDocumentRequest.objects.all()
        if candidate_id:
            queryset = queryset.filter(candidate_id=candidate_id)
        user = request.user
        # checking user level permissions
        perm = "recruitment.view_candidatedocumentrequest"
        # queryset = permission_based_queryset(user, perm, queryset, user_obj=True)
        # No employee_id on this model, so the helper's manager branch crashed
        # (500) for reporting managers without the permission.
        if not user.has_perm(perm):
            return queryset.none()
        return queryset

    def get(self, request, pk=None, candidate_id=None):
        if pk:
            doc_request = object_check(CandidateDocumentRequest, pk)
            if doc_request is None:
                return Response(
                    {"error": _("CandidateDocumentRequest not found")}, status=404
                )
            serializer = CandidateDocumentRequestSerializer(doc_request)
            return Response(serializer.data, status=200)

        doc_requests = self.get_queryset(request, candidate_id)
        paginator = PageNumberPagination()
        page = paginator.paginate_queryset(doc_requests, request)
        serializer = CandidateDocumentRequestSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)

    @permission_required("recruitment.add_candidatedocumentrequest")
    def post(self, request, candidate_id=None, **kwargs):
        data = request.data.copy()
        if (
            candidate_id
            and not data.get("candidate_id_write")
            and not data.get("candidate_id")
        ):
            data["candidate_id_write"] = candidate_id
        serializer = CandidateDocumentRequestSerializer(data=data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class CandidateDocumentRequestGetUpdateDeleteAPIView(RecruitmentAPIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        doc_request = object_check(CandidateDocumentRequest, pk)
        if doc_request is None:
            return Response(
                {"error": _("CandidateDocumentRequest not found")}, status=404
            )
        serializer = CandidateDocumentRequestSerializer(doc_request)
        return Response(serializer.data, status=200)

    @permission_required("recruitment.change_candidatedocumentrequest")
    def put(self, request, pk):
        doc_request = object_check(CandidateDocumentRequest, pk)
        if doc_request is None:
            return Response(
                {"error": _("CandidateDocumentRequest not found")}, status=404
            )
        serializer = CandidateDocumentRequestSerializer(
            doc_request, data=request.data, partial=True
        )
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=200)
        return Response(serializer.errors, status=400)

    @permission_required("recruitment.delete_candidatedocumentrequest")
    def delete(self, request, pk):
        doc_request = object_check(CandidateDocumentRequest, pk)
        if doc_request is None:
            return Response(
                {"error": _("CandidateDocumentRequest not found")}, status=404
            )
        try:
            doc_request.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)
        except Exception as e:
            return Response({"error": str(e)}, status=400)


# Candidate Document Views
class CandidateDocumentGetCreateAPIView(RecruitmentAPIView):
    permission_classes = [IsAuthenticated]
    queryset = CandidateDocument.objects.none()  # For drf-yasg schema generation

    def get_queryset(self, request=None, candidate_id=None, document_request_id=None):
        # Handle schema generation for DRF-YASG
        if getattr(self, "swagger_fake_view", False) or request is None:
            return CandidateDocument.objects.none()
        queryset = CandidateDocument.objects.all()
        if candidate_id:
            queryset = queryset.filter(candidate_id=candidate_id)
        if document_request_id:
            queryset = queryset.filter(document_request_id=document_request_id)
        user = request.user
        # checking user level permissions
        perm = "recruitment.view_candidatedocument"
        queryset = permission_based_queryset(user, perm, queryset, user_obj=True)
        return queryset

    def get(self, request, pk=None, candidate_id=None, document_request_id=None):
        if pk:
            document = object_check(CandidateDocument, pk)
            if document is None:
                return Response({"error": _("CandidateDocument not found")}, status=404)
            serializer = CandidateDocumentSerializer(document)
            return Response(serializer.data, status=200)

        documents = self.get_queryset(request, candidate_id, document_request_id)
        paginator = PageNumberPagination()
        page = paginator.paginate_queryset(documents, request)
        serializer = CandidateDocumentSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)

    @permission_required("recruitment.add_candidatedocument")
    def post(self, request, candidate_id=None, document_request_id=None, **kwargs):
        data = request.data.copy()
        if (
            candidate_id
            and not data.get("candidate_id_write")
            and not data.get("candidate_id")
        ):
            data["candidate_id_write"] = candidate_id
        if (
            document_request_id
            and not data.get("document_request_id_write")
            and not data.get("document_request_id")
        ):
            data["document_request_id_write"] = document_request_id
        # serializer = CandidateDocumentSerializer(data=data)
        # if serializer.is_valid():
        #     serializer.save()
        #     return Response(serializer.data, status=status.HTTP_201_CREATED)
        # return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        #
        # Same path as the UI's Documents tab: candidate access check, PDF by
        # content, 15 MB, audited.
        from recruitment.services import candidate as candidate_service
        from recruitment.services.errors import RecruitmentError

        target = data.get("candidate_id_write") or data.get("candidate_id")
        if not target:
            return Response({"error": _("candidate_id_write is required.")}, status=400)
        try:
            document = candidate_service.upload_document(
                request.user,
                target,
                request.FILES.get("document"),
                title=data.get("title"),
            )
        except RecruitmentError as error:
            return Response({"error": str(error), "code": error.code}, status=400)
        return Response(CandidateDocumentSerializer(document).data, status=status.HTTP_201_CREATED)


class CandidateDocumentGetUpdateDeleteAPIView(RecruitmentAPIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        document = object_check(CandidateDocument, pk)
        if document is None:
            return Response({"error": _("CandidateDocument not found")}, status=404)
        serializer = CandidateDocumentSerializer(document)
        return Response(serializer.data, status=200)

    # Not offered: candidate documents are permanent in the UI (no edit/delete).
    # @permission_required("recruitment.change_candidatedocument")
    # def put(self, request, pk):
    #     document = object_check(CandidateDocument, pk)
    #     if document is None:
    #         return Response({"error": _("CandidateDocument not found")}, status=404)
    #     serializer = CandidateDocumentSerializer(
    #         document, data=request.data, partial=True
    #     )
    #     if serializer.is_valid():
    #         serializer.save()
    #         return Response(serializer.data, status=200)
    #     return Response(serializer.errors, status=400)

    # Not offered: candidate documents are permanent in the UI (no edit/delete).
    # @permission_required("recruitment.delete_candidatedocument")
    # def delete(self, request, pk):
    #     document = object_check(CandidateDocument, pk)
    #     if document is None:
    #         return Response({"error": _("CandidateDocument not found")}, status=404)
    #     try:
    #         document.delete()
    #         return Response(status=status.HTTP_204_NO_CONTENT)
    #     except Exception as e:
    #         return Response({"error": str(e)}, status=400)
    #
    #
    # nkedIn Account Views


class LinkedInAccountGetCreateAPIView(RecruitmentAPIView):
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend]
    filterset_class = LinkedInAccountFilter
    queryset = LinkedInAccount.objects.none()  # For drf-yasg schema generation

    def get_queryset(self, request=None):
        # Handle schema generation for DRF-YASG
        if getattr(self, "swagger_fake_view", False) or request is None:
            return LinkedInAccount.objects.none()
        queryset = LinkedInAccount.objects.all()
        user = request.user
        # checking user level permissions
        perm = "recruitment.view_linkedinaccount"
        # queryset = permission_based_queryset(user, perm, queryset, user_obj=True)
        # No employee_id on this model, so the helper's manager branch crashed
        # (500) for reporting managers without the permission.
        if not user.has_perm(perm):
            return queryset.none()
        return queryset

    def get(self, request, pk=None):
        if pk:
            account = object_check(LinkedInAccount, pk)
            if account is None:
                return Response({"error": _("LinkedInAccount not found")}, status=404)
            serializer = LinkedInAccountSerializer(account)
            return Response(serializer.data, status=200)

        accounts = self.get_queryset(request)
        filterset = self.filterset_class(request.GET, queryset=accounts)

        # pagination section
        paginator = PageNumberPagination()
        page = paginator.paginate_queryset(filterset.qs, request)
        serializer = LinkedInAccountSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)

    @permission_required("recruitment.add_linkedinaccount")
    def post(self, request, **kwargs):
        serializer = LinkedInAccountSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class LinkedInAccountGetUpdateDeleteAPIView(RecruitmentAPIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        account = object_check(LinkedInAccount, pk)
        if account is None:
            return Response({"error": _("LinkedInAccount not found")}, status=404)
        serializer = LinkedInAccountSerializer(account)
        return Response(serializer.data, status=200)

    @permission_required("recruitment.change_linkedinaccount")
    def put(self, request, pk):
        account = object_check(LinkedInAccount, pk)
        if account is None:
            return Response({"error": _("LinkedInAccount not found")}, status=404)
        serializer = LinkedInAccountSerializer(account, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=200)
        return Response(serializer.errors, status=400)

    @permission_required("recruitment.delete_linkedinaccount")
    def delete(self, request, pk):
        account = object_check(LinkedInAccount, pk)
        if account is None:
            return Response({"error": _("LinkedInAccount not found")}, status=404)
        try:
            account.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)
        except Exception as e:
            return Response({"error": str(e)}, status=400)


# ---------------------------------------------------------------------------
# Screening questions (Feature 2)
# ---------------------------------------------------------------------------


class JobOpeningQuestionAPIView(RecruitmentAPIView):
    """
    Read-only access to a published job opening's frozen screening questions.

        GET /api/recruitment/job-opening/<pk>/questions/

    Read-only by design: a published snapshot is historical record. There is
    deliberately no POST/PATCH/PUT/DELETE here, so no client can rewrite the
    question a candidate actually answered.

    Authorization is the Feature 1 rule -- Django permission plus company and
    object scope -- resolved by
    recruitment.services.authorization.get_job_opening_for_user, so supplying
    another tenant's job opening id returns 404 (never 403, which would confirm
    the object exists) and an in-scope opening the user may not manage returns
    403.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        from recruitment.services.authorization import get_job_opening_for_user
        from recruitment.services.errors import (
            JobOpeningNotFound,
            RecruitmentPermissionDenied,
        )
        from recruitment.services.screening import published_questions

        try:
            job_opening = get_job_opening_for_user(
                request.user, pk, "recruitment.view_recruitment"
            )
        except JobOpeningNotFound as error:
            return Response({"error": str(error), "code": error.code}, status=404)
        except RecruitmentPermissionDenied as error:
            return Response({"error": str(error), "code": error.code}, status=403)

        serializer = JobOpeningQuestionSerializer(
            published_questions(job_opening), many=True
        )
        return Response(serializer.data, status=200)


class CandidateScreeningAnswerAPIView(RecruitmentAPIView):
    """
    Read a candidate's screening answers.

        GET /api/recruitment/candidate/<pk>/screening-answers/

    Scoped through the candidate's job opening, so Company A cannot read
    Company B's answers by changing the candidate id. select_related keeps this
    to a single query regardless of answer count.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        from recruitment.services.authorization import get_job_opening_for_user
        from recruitment.services.errors import (
            JobOpeningNotFound,
            RecruitmentPermissionDenied,
        )
        from recruitment.services.screening import answers_for_candidate

        candidate = Candidate.objects.filter(pk=pk).first()
        if candidate is None or candidate.recruitment_id_id is None:
            return Response({"error": _("Candidate not found")}, status=404)

        # Authorize against the candidate's job opening rather than the
        # candidate row, so company scope is enforced the same way everywhere.
        try:
            get_job_opening_for_user(
                request.user,
                candidate.recruitment_id_id,
                "recruitment.view_candidate",
            )
        except JobOpeningNotFound as error:
            return Response({"error": str(error), "code": error.code}, status=404)
        except RecruitmentPermissionDenied as error:
            return Response({"error": str(error), "code": error.code}, status=403)

        serializer = CandidateAnswerSerializer(
            answers_for_candidate(candidate), many=True
        )
        return Response(serializer.data, status=200)
