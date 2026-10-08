"""
recruitment/services/interview.py

Interviews scheduled in Google Calendar (PRD: no scheduling screen in Krew).

"Schedule Interview" opens a prefilled Google Calendar event carrying a Krew
reference in its description. Once the user saves it in Google, Krew finds
that event in the scheduler's calendar -- through the Google account they
connected in the Google Meet module -- and stores its Meet link and time
(InterviewMeeting), so "Join Meeting" works from Krew. The lookup runs when
the user clicks "Fetch Meeting Link".
"""

import logging
import secrets
from datetime import datetime, timedelta, timezone as dt_timezone

from django.apps import apps
from django.utils import timezone as tz
from django.utils.dateparse import parse_date, parse_datetime
from django.utils.formats import date_format

from recruitment.services.audit import RecruitmentAuditService
from recruitment.services.errors import (
    GoogleCalendarNotConnected,
    GoogleCalendarUnavailable,
    RecruitmentPermissionDenied,
)

logger = logging.getLogger(__name__)

def _new_reference():
    from recruitment.models import InterviewMeeting

    while True:
        reference = "KREW-INT-" + secrets.token_hex(4).upper()
        if not InterviewMeeting.objects.entire().filter(reference=reference).exists():
            return reference


def current_meeting(candidate):
    """This candidate's interview at their current stage, if any."""
    from recruitment.models import InterviewMeeting

    if candidate is None or candidate.stage_id_id is None:
        return None
    return (
        InterviewMeeting.objects.entire()
        .filter(candidate=candidate, stage_id=candidate.stage_id_id)
        .first()
    )


def start_scheduling(user, candidate):
    """
    The InterviewMeeting the Google event will be tied to (created on first
    click, reused after), and the Calendar URL to open.
    """
    from recruitment.models import InterviewMeeting
    from recruitment.services.candidate import stage_move_authority

    if stage_move_authority(user, candidate) is None or candidate.stage_id_id is None:
        raise RecruitmentPermissionDenied()

    employee = getattr(user, "employee_get", None)
    meeting = current_meeting(candidate)
    if meeting is None:
        meeting = InterviewMeeting.objects.create(
            candidate=candidate,
            stage_id=candidate.stage_id_id,
            reference=_new_reference(),
            scheduled_by=employee,
        )
    elif meeting.status != InterviewMeeting.Status.LINKED and employee is not None:
        # Whoever schedules is whose calendar is searched.
        # A cancelled event is forgotten so the new one is searched afresh.
        meeting.scheduled_by = employee
        meeting.status = InterviewMeeting.Status.PENDING
        meeting.google_event_id = ""
        meeting.save(update_fields=["scheduled_by", "status", "google_event_id"])
    return meeting, candidate.get_google_calendar_url(
        reference=meeting.reference, account=google_account_email(employee)
    )


def google_account_email(employee):
    """
    The Google account the scheduler uses: the one they connected to Krew
    (so the event lands in the calendar Krew reads), else their work email.
    """
    if employee is None:
        return ""
    if google_status(employee) == "ok":
        try:
            calendar = _calendar(employee).calendars().get(calendarId="primary").execute()
            if calendar.get("id"):
                return calendar["id"]
        except Exception:
            logger.warning("Could not read Google account for employee %s", employee.pk)
    work = getattr(employee, "employee_work_info", None)
    return (getattr(work, "email", "") or employee.email or "").strip()


def google_status(employee):
    """'ok', 'not_connected' (user), or 'unavailable' (company/app)."""
    if not apps.is_installed("horilla_meet") or employee is None:
        return "unavailable"
    from horilla_meet.models import GoogleCloudCredential, GoogleCredential

    if not GoogleCredential.objects.filter(employee_id=employee).exists():
        company = employee.get_company() if hasattr(employee, "get_company") else None
        if not GoogleCloudCredential.objects.filter(company_id=company).exists():
            return "unavailable"
        return "not_connected"
    return "ok"


def _calendar(employee):
    """A Calendar API client for this employee's connected Google account."""
    status = google_status(employee)
    if status == "unavailable":
        raise GoogleCalendarUnavailable()
    if status == "not_connected":
        raise GoogleCalendarNotConnected()

    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    from horilla_meet.models import GoogleCredential

    stored = GoogleCredential.objects.get(employee_id=employee)
    credentials = stored.to_google_credentials()
    if credentials.expiry is not None and credentials.expiry.tzinfo is not None:
        # google-auth compares expiry against a naive UTC clock.
        credentials.expiry = credentials.expiry.astimezone(dt_timezone.utc).replace(tzinfo=None)
    if not credentials.valid:
        try:
            credentials.refresh(Request())
        except Exception:
            logger.warning("Google token refresh failed for employee %s", employee.pk)
            raise GoogleCalendarNotConnected()
        stored.token = credentials.token
        if credentials.expiry is not None:
            stored.expires_at = credentials.expiry.replace(tzinfo=dt_timezone.utc)
        stored.save(update_fields=["token", "expires_at"])
    return build("calendar", "v3", credentials=credentials, cache_discovery=False)


def _meet_link(event):
    if event.get("hangoutLink"):
        return event["hangoutLink"]
    for entry in (event.get("conferenceData") or {}).get("entryPoints", []):
        if entry.get("entryPointType") == "video" and entry.get("uri"):
            return entry["uri"]
    return ""


def _start(event):
    start = event.get("start") or {}
    if start.get("dateTime"):
        return parse_datetime(start["dateTime"])
    if start.get("date"):
        day = parse_date(start["date"])
        return tz.make_aware(datetime(day.year, day.month, day.day)) if day else None
    return None


def _find_event(service, meeting):
    """The calendar event carrying this meeting's reference, or None."""
    if meeting.google_event_id:
        try:
            return (
                service.events()
                .get(calendarId="primary", eventId=meeting.google_event_id)
                .execute()
            )
        except Exception:
            return {"status": "cancelled"}
    result = (
        service.events()
        .list(
            calendarId="primary",
            q=meeting.reference,
            timeMin=(meeting.created_at - timedelta(days=1)).isoformat(),
            singleEvents=True,
            maxResults=10,
        )
        .execute()
    )
    for event in result.get("items", []):
        text = f"{event.get('description', '')} {event.get('summary', '')}"
        if meeting.reference in text:
            return event
    return None


def sync_meeting(meeting, actor=None):
    """
    Read the meeting's Google event and update Krew. Returns the meeting.
    Raises GoogleCalendarNotConnected / GoogleCalendarUnavailable.
    """
    from recruitment.models import InterviewMeeting, RecruitmentAuditEvent

    event = _find_event(_calendar(meeting.scheduled_by), meeting)
    meeting.last_checked_at = tz.now()
    fields = ["last_checked_at"]
    audit = None

    if event is None:
        pass  # not saved in Google yet
    elif event.get("status") == "cancelled":
        if meeting.status == InterviewMeeting.Status.LINKED:
            meeting.status = InterviewMeeting.Status.CANCELLED
            fields.append("status")
            audit = RecruitmentAuditEvent.EventType.INTERVIEW_CANCELLED
    else:
        start = _start(event)
        was_linked = meeting.status == InterviewMeeting.Status.LINKED
        moved = was_linked and start and meeting.start_at and start != meeting.start_at
        meeting.google_event_id = event.get("id", "")
        meeting.meet_url = _meet_link(event)
        meeting.event_url = event.get("htmlLink", "")[:500]
        meeting.start_at = start
        meeting.status = InterviewMeeting.Status.LINKED
        fields += ["google_event_id", "meet_url", "event_url", "start_at", "status"]
        if not was_linked:
            audit = RecruitmentAuditEvent.EventType.INTERVIEW_SCHEDULED
        elif moved:
            audit = RecruitmentAuditEvent.EventType.INTERVIEW_RESCHEDULED

    meeting.save(update_fields=fields)
    if audit:
        candidate = meeting.candidate
        RecruitmentAuditService.record(
            event_type=audit,
            actor=actor,
            system=actor is None,
            company=candidate.company_id,
            job_opening=candidate.recruitment_id,
            candidate=candidate,
            stage=meeting.stage,
            object_type="InterviewMeeting",
            object_id=meeting.pk,
            details={
                "reference": meeting.reference,
                "start_at": meeting.start_at.isoformat() if meeting.start_at else None,
                # Shown in the candidate's History.
                "when": (
                    date_format(tz.localtime(meeting.start_at), "j M Y, H:i")
                    if meeting.start_at
                    else ""
                ),
                "has_meet_link": bool(meeting.meet_url),
            },
        )
    return meeting
