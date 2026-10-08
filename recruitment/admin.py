"""
admin.py

This page is used to register the model with admins site.
"""

from django.contrib import admin

from recruitment.models import (
    Candidate,
    CandidateAnswer,
    CandidateRating,
    InterviewSchedule,
    JobOpeningQuestion,
    LinkedInAccount,
    Recruitment,
    RecruitmentAuditEvent,
    RecruitmentSurvey,
    RecruitmentSurveyAnswer,
    RejectedCandidate,
    SkillZone,
    Stage,
    SurveyTemplateQuestion,
)

# Register your models here.


@admin.register(Recruitment)
class RecruitmentAdmin(admin.ModelAdmin):
    """
    Job openings in Django admin.

    Lifecycle fields are read-only here on purpose. Admin writes bypass the
    lifecycle service entirely, so leaving `status` (or its mirrors)
    editable would let a staff user jump a job opening straight to
    PUBLISHED with no permission check, no transition validation and no
    audit event. Lifecycle changes go through
    recruitment.services.job_opening only.
    """

    list_display = ("title", "company_id", "status", "start_date", "end_date", "vacancy")
    list_filter = ("status", "company_id", "closed", "is_published")
    search_fields = ("title",)
    readonly_fields = (
        "status",
        "is_published",
        "closed",
        "is_active",
        "submitted_for_review_at",
        "submitted_for_review_by",
        "published_at",
        "published_by",
        "closed_at",
        "closed_by",
        "removed_at",
        "removed_by",
    )


@admin.register(RecruitmentAuditEvent)
class RecruitmentAuditEventAdmin(admin.ModelAdmin):
    """
    Business-event audit trail: visible, never editable.

    Add/change/delete are all refused, so the trail cannot be doctored from
    admin even by a superuser. The model itself also refuses updates and
    deletes at save()/delete(), so this is defence in depth rather than the
    only guard.
    """

    list_display = (
        "timestamp",
        "event_type",
        "actor",
        "company_id",
        "job_opening",
        "candidate",
    )
    list_filter = ("event_type", "company_id")
    search_fields = ("object_type", "event_type")
    date_hierarchy = "timestamp"

    def get_readonly_fields(self, request, obj=None):
        return [field.name for field in self.model._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(JobOpeningQuestion)
class JobOpeningQuestionAdmin(admin.ModelAdmin):
    """
    Frozen screening questions: visible, never editable.

    A published snapshot is historical record. Admin writes bypass the service
    layer, so allowing an edit here would let a staff user rewrite the question
    a candidate actually answered. The model refuses updates and deletes at
    save()/delete() too -- this is defence in depth.
    """

    list_display = (
        "job_opening",
        "display_order",
        "wording",
        "question_type",
        "is_mandatory",
        "is_reconstructed",
    )
    list_filter = ("is_mandatory", "is_reconstructed", "question_type")
    search_fields = ("wording",)

    def get_readonly_fields(self, request, obj=None):
        return [field.name for field in self.model._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(SurveyTemplateQuestion)
class SurveyTemplateQuestionAdmin(admin.ModelAdmin):
    """Per-template question configuration (mandatory/optional, ordering)."""

    list_display = ("surveytemplate", "recruitmentsurvey", "is_mandatory", "sequence")
    list_filter = ("is_mandatory", "surveytemplate")


admin.site.register(CandidateAnswer)
admin.site.register(Stage)
admin.site.register(Candidate)
admin.site.register(RejectedCandidate)
admin.site.register(RecruitmentSurveyAnswer)
admin.site.register(RecruitmentSurvey)
admin.site.register(CandidateRating)
admin.site.register(SkillZone)
admin.site.register(InterviewSchedule)
admin.site.register(LinkedInAccount)
