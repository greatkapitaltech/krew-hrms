"""
recruitment/sidebar.py

To set Horilla sidebar for onboarding
"""

from django.apps import apps
from django.contrib.auth.context_processors import PermWrapper
from django.urls import reverse, reverse_lazy
from django.utils.translation import gettext_lazy as _

from horilla.menu import settings_menu
from recruitment.models import InterviewSchedule
from recruitment.templatetags.recruitmentfilters import is_stagemanager

MENU = _("Recruitment")
ACCESSIBILITY = "recruitment.sidebar.menu_accessibilty"
IMG_SRC = "images/ui/recruitment.svg"

SUBMENUS = [
    # Hidden per PRD: no recruitment dashboard; the entry screen is Job Openings.
    # {
    #     "menu": _("Dashboard"),
    #     "redirect": reverse("recruitment-dashboard"),
    #     "accessibility": "recruitment.sidebar.recruitment_accessibility",
    # },
    {
        "menu": _("Recruitment Pipeline"),
        "redirect": reverse("cbv-pipeline"),
        "accessibility": "recruitment.sidebar.pipeline_accessibility",
    },
    {
        "menu": _("Open Recruitments"),
        "redirect": reverse("open-recruitments"),
        "accessibility": "recruitment.sidebar.recruitment_accessibility",
    },
    # Hidden per PRD: Candidate Pool replaces Candidates; interviews are scheduled in Google Calendar.
    # {
    #     "menu": _("Candidates"),
    #     "redirect": reverse("candidate-view"),
    #     "accessibility": "recruitment.sidebar.candidates_accessibility",
    #     "match_prefixes": ["/recruitment/candidate-update/"],
    # },
    # {
    #     "menu": _("Interviews"),
    #     "redirect": reverse("interview-view"),
    #     "accessibility": "recruitment.sidebar.interview_accessibility",
    # },
    {
        "menu": _("Job Openings"),
        "redirect": reverse("recruitment-view"),
        "accessibility": "recruitment.sidebar.recruitment_accessibility",
    },
    {
        "menu": _("Screening Questions"),
        "redirect": reverse("recruitment-survey-question-template-view"),
        "accessibility": "recruitment.sidebar.survey_accessibility",
    },
    {
        # The permanent, company-scoped pool of every candidate/application.
        "menu": _("Candidate Pool"),
        "redirect": reverse("candidate-pool"),
        "accessibility": "recruitment.sidebar.candidate_pool_accessibility",
    },
    # Hidden per PRD: Talent Pool (Skill Zone) is Phase 2.
    # {
    #     "menu": _("Talent Pool"),
    #     "redirect": reverse("skill-zone-view"),
    #     "accessibility": "recruitment.sidebar.skill_zone_accessibility",
    # },
    # Hidden: Configuration menu not needed.
    # {
    #     "menu": _("Configuration"),
    #     "redirect": reverse("recruitment-settings-view"),
    #     "accessibility": "recruitment.sidebar.recruitment_settings_accessibility",
    # },
]


def menu_accessibilty(
    request, _menu: str = "", user_perms: PermWrapper = [], *args, **kwargs
) -> bool:
    return is_stagemanager(request.user) or "recruitment" in user_perms


def pipeline_accessibility(
    request, _submenu: dict = {}, user_perms: PermWrapper = [], *args, **kwargs
) -> bool:
    _submenu["redirect"] = _submenu["redirect"] + "?closed=false"
    return is_stagemanager(request.user) or request.user.has_perm(
        "recruitment.view_recruitment"
    )


def candidates_accessibility(
    request, _submenu: dict = {}, user_perms: PermWrapper = [], *args, **kwargs
) -> bool:
    return request.user.has_perm("recruitment.view_candidate")


def candidate_pool_accessibility(
    request, _submenu: dict = {}, user_perms: PermWrapper = [], *args, **kwargs
) -> bool:
    """
    Candidate Pool is gated on the existing view_candidate permission.

    No new permission is introduced: group grants are resolved by action prefix
    (base/signals.py), so adding e.g. view_candidatepool would be granted
    automatically to every role configured with "view" actions -- a silent
    widening. Hiding this menu is presentation only; every Pool view and service
    re-checks the permission server-side.
    """
    # Pool is for drive-level roles (PRD); a Stage Manager works from the
    # pipeline of the stages they own.
    return request.user.has_perm("recruitment.view_candidate") and request.user.has_perm(
        "recruitment.view_recruitment"
    )


def survey_accessibility(
    request, _submenu: dict = {}, user_perms: PermWrapper = [], *args, **kwargs
) -> bool:
    _submenu["redirect"] = _submenu["redirect"] + "?closed=false"
    # Permission only: managing a drive does not by itself open the bank.
    # Was: is_recruitmentmangers(request.user) or request.user.has_perm(...)
    return request.user.has_perm("recruitment.view_recruitmentsurvey")


def recruitment_accessibility(
    request, _submenu: dict = {}, user_perms: PermWrapper = [], *args, **kwargs
) -> bool:
    return request.user.has_perm("recruitment.view_recruitment")


def interview_accessibility(
    request, _submenu: dict = {}, user_perms: PermWrapper = [], *args, **kwargs
) -> bool:
    employee = getattr(request.user, "employee_get", None)
    view_interview = (
        bool(employee)
        and InterviewSchedule.objects.filter(employee_id=employee).exists()
    )

    return request.user.has_perm("recruitment.view_interviewschedule") or view_interview


def skill_zone_accessibility(
    request, _submenu: dict = {}, user_perms: PermWrapper = [], *args, **kwargs
) -> bool:
    return is_stagemanager(request.user) or request.user.has_perm(
        "recruitment.view_skillzone"
    )


def recruitment_settings_accessibility(
    request, _submenu: dict = {}, user_perms: PermWrapper = [], *args, **kwargs
) -> bool:
    return (
        request.user.has_perm("recruitment.view_rejectreason")
        or request.user.has_perm("recruitment.view_recruitment")
        or request.user.has_perm("recruitment.view_stage")
    )


def dashboard_accessibility(request, submenu, user_perms, *args, **kwargs):
    return is_stagemanager(request.user) or "recruitment" in user_perms


# ---------------------------------------------------------------------------
# Settings menu registrations
# ---------------------------------------------------------------------------


def self_tracking_accessibility(request, submenu, user_perms, *args, **kwargs):
    return request.user.has_perm("recruitment.view_recruitment")


# Hidden per PRD: the Candidate Portal (self-tracking, rating visibility) is
# Phase 2, so Settings has no Recruitment section.
# @settings_menu.register
# class RecruitmentSettings:
#     title = _("Recruitment")
#     order = 4
#     condition = lambda self, request: apps.is_installed("recruitment")
#     items = [
#         {
#             "label": _("Candidate Portal"),
#             "url": reverse_lazy("self-tracking-feature"),
#             "accessibility": self_tracking_accessibility,
#             "search_entries": [
#                 {
#                     "text": _("Application Tracking"),
#                     "description": _(
#                         "Allow candidates to track their recruitment pipeline status"
#                     ),
#                 },
#                 {
#                     "text": _("Rating Visibility"),
#                     "description": _(
#                         "Allow candidates to view their recruitment rating"
#                     ),
#                 },
#             ],
#         },
#     ]
