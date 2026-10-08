"""
Interviews scheduled in Google Calendar, with the Meet link brought back to
Krew (see recruitment.services.interview).
"""

from django.contrib import messages
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.formats import date_format
from django.utils.timezone import localtime
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_http_methods

from horilla.decorators import login_required
from horilla.http import HorillaRedirect
from recruitment.services import candidate as candidate_service
from recruitment.services import interview as interview_service
from recruitment.services.errors import (
    GoogleCalendarNotConnected,
    RecruitmentError,
)


@login_required
def schedule_interview(request, cand_id):
    """
    "Schedule Interview": tie the event to this candidate's stage, then open
    the prefilled Google Calendar event (PRD: scheduling happens in Google).
    """
    try:
        candidate = candidate_service.get_candidate_for_user(request.user, cand_id)
        _meeting, calendar_url = interview_service.start_scheduling(
            request.user, candidate
        )
    except RecruitmentError as error:
        messages.error(request, str(error))
        return redirect("cbv-pipeline")

    if (
        interview_service.google_status(getattr(request.user, "employee_get", None))
        == "not_connected"
    ):
        # Scheduling still works; only the automatic link needs the connection.
        messages.info(
            request,
            _(
                "Connect your Google account (Fetch Meeting Link on the candidate) "
                "so Krew can show the meeting link."
            ),
        )
    return redirect(calendar_url)


@login_required
@require_http_methods(["POST"])
def refresh_meeting(request, pk):
    """"Fetch Meeting Link": look the event up in Google now."""
    from django.http import HttpResponse

    from recruitment.models import InterviewMeeting

    meeting = InterviewMeeting.objects.filter(pk=pk).select_related(
        "candidate", "scheduled_by"
    ).first()
    try:
        if meeting is None:
            raise RecruitmentError(_("Interview not found."))
        # Same authority as scheduling (and company-scoped by the lookup).
        candidate_service.get_candidate_for_user(request.user, meeting.candidate_id)
        if candidate_service.stage_move_authority(request.user, meeting.candidate) is None:
            raise RecruitmentError(_("You cannot manage this candidate's interview."))
        interview_service.sync_meeting(meeting, actor=request.user)
    except GoogleCalendarNotConnected as error:
        mine = meeting is not None and meeting.scheduled_by_id == getattr(
            getattr(request.user, "employee_get", None), "pk", None
        )
        if mine:
            # Send them through Google sign-in (Google Meet module).
            response = HttpResponse(status=204)
            response["HX-Redirect"] = reverse("authenticate-gmeet")
            return response
        messages.error(
            request,
            _("%(name)s, who scheduled this interview, needs to connect their Google account.")
            % {"name": meeting.scheduled_by},
        )
        return HorillaRedirect(request)
    except RecruitmentError as error:
        messages.error(request, str(error))
        return HorillaRedirect(request)

    if meeting.status == InterviewMeeting.Status.LINKED:
        when = (
            date_format(localtime(meeting.start_at), "j M Y, H:i")
            if meeting.start_at
            else ""
        )
        messages.success(request, _("Meeting link added. %(when)s") % {"when": when})
    elif meeting.status == InterviewMeeting.Status.CANCELLED:
        messages.warning(request, _("This interview was deleted in Google Calendar."))
    else:
        messages.info(
            request,
            _(
                "No saved event found yet. Save the event in Google Calendar "
                "(keep the Krew reference line), then try again."
            ),
        )
    return HorillaRedirect(request)
