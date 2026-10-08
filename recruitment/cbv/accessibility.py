"""
Accessibility
"""

from django.contrib.auth.context_processors import PermWrapper

from base.methods import check_manager
from employee.models import Employee
from horilla_auth.models import HorillaUser
from recruitment.methods import (
    in_all_managers,
    is_recruitmentmanager,
    is_stagemanager,
    stage_manages,
)
from recruitment.models import Candidate, RecruitmentGeneralSetting, RejectedCandidate


def hiring_handoff_accessibility(request, instance, user_perm):
    """
    The hiring handoff tab belongs to the Final HR Round only.

    This decides whether the tab is OFFERED, nothing more.
    recruitment.views.candidate_pool.candidate_handoff_tab re-checks company
    scope, object-level authority and the status before it writes anything --
    hiding a tab is not access control.
    """
    from recruitment.services.candidate import STATUS_FINAL_HR_ROUND, candidate_status

    if candidate_status(instance) != STATUS_FINAL_HR_ROUND:
        return False
    return request.user.has_perm("recruitment.change_candidate")


def convert_emp(request, instance, user_perm):
    """
    Covert employee accessibility
    """
    mails = list(Candidate.objects.values_list("email", flat=True))
    existing_emails = list(
        HorillaUser.objects.filter(username__in=mails).values_list("email", flat=True)
    )
    if not instance.email in existing_emails and not instance.start_onboard:
        return True


def add_skill_zone(request, instance, user_perm):
    """
    Add talent pool  accessibility
    """

    mails = list(Candidate.objects.values_list("email", flat=True))
    existing_emails = list(
        HorillaUser.objects.filter(username__in=mails).values_list("email", flat=True)
    )
    if not instance.email in existing_emails and request.user.has_perm(
        "recruitment.add_skillzonecandidate"
    ):
        return True


def add_reject(request, instance, user_perm):
    """
    add reject accessibility
    """
    first = RejectedCandidate.objects.filter(candidate_id=instance).first()
    mails = list(Candidate.objects.values_list("email", flat=True))
    existing_emails = list(
        HorillaUser.objects.filter(username__in=mails).values_list("email", flat=True)
    )
    if not instance.email in existing_emails:
        if request.user.has_perm(
            "recruitment.add_rejectedcandidate"
        ) or is_stagemanager(request):
            if not first:
                return True


def edit_reject(request, instance, user_perm):
    """
    Edit reject accessibility
    """
    first = RejectedCandidate.objects.filter(candidate_id=instance).first()
    mails = list(Candidate.objects.values_list("email", flat=True))
    existing_emails = list(
        HorillaUser.objects.filter(username__in=mails).values_list("email", flat=True)
    )
    if not instance.email in existing_emails:
        if request.user.has_perm(
            "recruitment.add_rejectedcandidate"
        ) or is_stagemanager(request):
            if first:
                return True


def archive_status(request, instance, user_perm):
    """
    To acces archive in list candidates
    """
    if instance.is_active:
        return True


def unarchive_status(request, instance, user_perm):
    """
    To acces un-archive in list candidates
    """
    if not instance.is_active:
        return True


def onboarding_accessibility(
    request, instance: object = None, user_perms: PermWrapper = [], *args, **kwargs
) -> bool:
    """
    accessibility for onboarding tab in candidate individual view
    """
    candidate = Candidate.objects.get(pk=instance.pk)
    if (
        candidate.cand_onboarding_task.exists()
        and in_all_managers(request)
        or request.user.has_perm("onboarding.view_onboardingtask")
    ):
        return True
    return False


def rating_accessibility(
    request, instance: object = None, user_perms: PermWrapper = [], *args, **kwargs
) -> bool:
    """
    accessebility for rating tab in candidate individual view
    """
    candidate = Candidate.objects.get(pk=instance.pk)
    stage_manage = stage_manages(request.user.employee_get, candidate.recruitment_id)
    if (
        stage_manage
        or request.user.has_perm("recruitment.view_candidate")
        or request.user.has_perm("recruitment.view_candidate")
    ):
        return True
    return False


def if_manager_accessibility(request, instance, *args, **kwargs):
    """
    If manager accessibility
    """
    return (
        is_recruitmentmanager(request)
        or is_stagemanager(request)
        or request.user.has_perm("recruitment.view_candidate")
    )


def empl_scheduled_interview_accessibility(
    request, instance: object = None, user_perms: PermWrapper = [], *args, **kwargs
) -> bool:
    """
    sheduled interview tab accessibility for candidate individual view, employee individual view and employee profile
    """
    employee = Employee.objects.get(id=instance.pk)
    if (
        request.user.has_perm("recruitment.view_interviewschedule")
        or check_manager(request.user.employee_get, instance)
        or request.user == employee.employee_user_id
        or is_recruitmentmanager(request)
    ):
        return True
    return False


def view_candidate_self_tracking(request, instance, *args, **kwargs):
    if (
        request.user.has_perm("recruitment.view_candidate")
        or is_stagemanager(request)
        or is_recruitmentmanager(request)
    ):
        return True


def request_document(request, instance, *args, **kwargs):
    if (
        request.user.has_perm("recruitment.change_candidate")
        or request.user.has_perm("recruitment.add_candidatedocumentrequest")
        or is_stagemanager(request)
        or is_recruitmentmanager(request)
    ):
        return True


def check_candidate_self_tracking(request, instance, user_perm):
    """
    This method is used to get the candidate self tracking is enabled or not
    """
    selected_company = request.session.get("selected_company")
    if selected_company and selected_company != "all":
        setting = RecruitmentGeneralSetting.objects.filter(
            company_id_id=selected_company
        ).first()
    else:
        setting = RecruitmentGeneralSetting.objects.filter(
            company_id__isnull=True
        ).first()
    return setting.candidate_self_tracking if setting else False


def move_backward_accessibility(request, instance=None, user_perms=[], *args, **kwargs):
    """Move Backward: drive Managers / HR only, when there is a stage to go back to."""
    from recruitment.services.candidate import can_move_backward

    return instance is not None and can_move_backward(request.user, instance)


def _candidate_authority(request, instance):
    """Who may act on THIS candidate: drive Manager/HR, or their stage's manager."""
    from recruitment.services.candidate import (
        STATUS_REJECTED,
        candidate_status,
        stage_move_authority,
    )

    if instance is None or candidate_status(instance) == STATUS_REJECTED:
        return None
    return stage_move_authority(request.user, instance)


def move_forward_accessibility(request, instance=None, user_perms=[], *args, **kwargs):
    """Move Forward: own stage (Stage Manager) or any stage (Manager); not once hired."""
    from recruitment.services.candidate import next_stage_for

    if _candidate_authority(request, instance) is None or instance.hired:
        return False
    return next_stage_for(instance) is not None


def stage_action_accessibility(request, instance=None, user_perms=[], *args, **kwargs):
    """Reject / Schedule Interview: active (not hired, not rejected) candidates only."""
    return _candidate_authority(request, instance) is not None and not instance.hired


def schedule_interview_accessibility(request, instance=None, user_perms=[], *args, **kwargs):
    """Schedule Interview: as Reject, until this stage's interview is in Google."""
    if not stage_action_accessibility(request, instance):
        return False
    meeting = instance.get_interview_meeting()
    return meeting is None or meeting.status != meeting.Status.LINKED


def join_meeting_accessibility(request, instance=None, user_perms=[], *args, **kwargs):
    """Join Meeting: this stage's interview is in Google with a link."""
    if _candidate_authority(request, instance) is None:
        return False
    meeting = instance.get_interview_meeting()
    return bool(meeting and meeting.status == meeting.Status.LINKED and meeting.join_url)


def fetch_meeting_accessibility(request, instance=None, user_perms=[], *args, **kwargs):
    """Fetch Meeting Link: scheduled from Krew, not yet found in Google."""
    if _candidate_authority(request, instance) is None:
        return False
    meeting = instance.get_interview_meeting()
    if not (meeting and meeting.status == meeting.Status.PENDING):
        return False
    # Hidden where Google is not set up for the company: it could only fail.
    from recruitment.services.interview import google_status

    return google_status(meeting.scheduled_by) != "unavailable"


def email_candidate_accessibility(request, instance=None, user_perms=[], *args, **kwargs):
    """Email Candidate: anyone responsible for the candidate, hired included."""
    return _candidate_authority(request, instance) is not None
