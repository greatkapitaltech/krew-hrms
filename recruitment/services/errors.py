"""
recruitment/services/errors.py

Business errors raised by the Recruitment service layer.

These carry a user-safe message only -- never a stack trace, SQL fragment or
internal identifier -- because callers surface ``str(exc)`` straight to the
user (via ``messages.error`` in views, or a JSON ``{"error": ...}`` body in
the API). Anything an operator needs for debugging goes to the application
log at the raise site, not into the message.
"""

from django.utils.translation import gettext_lazy as _


class RecruitmentError(Exception):
    """Base class for every Recruitment business error."""

    # Overridden per subclass so API callers get a stable, greppable code
    # instead of having to string-match the human-readable message.
    code = "recruitment_error"
    default_message = _("The operation could not be completed.")

    def __init__(self, message=None):
        self.message = message or self.default_message
        super().__init__(self.message)

    def __str__(self):
        return str(self.message)


class RecruitmentPermissionDenied(RecruitmentError):
    """The user is authenticated but not allowed to perform the operation."""

    code = "permission_denied"
    default_message = _("You don't have permission to perform this action.")


class JobOpeningNotFound(RecruitmentError):
    """
    The job opening does not exist, or belongs to another company.

    Deliberately the same error for both cases: telling a user that an object
    exists but belongs to someone else is itself a cross-tenant information
    leak, so out-of-scope objects are indistinguishable from missing ones.
    """

    code = "job_opening_not_found"
    default_message = _("No job opening found matching the query.")


class InvalidLifecycleTransition(RecruitmentError):
    """The requested status change is not a permitted transition."""

    code = "invalid_transition"
    default_message = _("This job opening cannot move to that state.")


class PublicationValidationError(RecruitmentError):
    """The job opening is missing information required to publish."""

    code = "publication_validation_failed"
    default_message = _("This job opening is not ready to be published.")

    def __init__(self, message=None, blockers=None):
        # blockers is a list of human-readable reasons, rendered as a bullet
        # list by the UI. Kept separate from `message` so the caller can show
        # either a summary or the full list.
        self.blockers = list(blockers or [])
        if message is None and self.blockers:
            message = _("This job opening is not ready to be published: %(reasons)s") % {
                "reasons": "; ".join(str(blocker) for blocker in self.blockers)
            }
        super().__init__(message)


class ScreeningAnswerValidationError(RecruitmentError):
    """
    A candidate submission left a mandatory screening question unanswered.

    ``missing_questions`` holds the wording of every unanswered mandatory
    question so the candidate can be shown all of them at once instead of
    discovering them one submission at a time. Mandatory-ness is always read
    from the published snapshot, never from the reusable question.
    """

    code = "screening_answer_validation_failed"
    default_message = _("Please answer all mandatory questions.")

    def __init__(self, message=None, missing_questions=None):
        self.missing_questions = list(missing_questions or [])
        if message is None and self.missing_questions:
            message = _("Please answer all mandatory questions: %(questions)s") % {
                "questions": "; ".join(str(q) for q in self.missing_questions)
            }
        super().__init__(message)


class JobOpeningNotAcceptingCandidates(RecruitmentError):
    """
    A new candidate/application was aimed at a job opening that is not open.

    Only PUBLISHED openings accept new candidates. DRAFT and REVIEW are not
    live yet; CLOSED and REMOVED are past it. Candidates already attached to
    the opening are unaffected -- this guards creation only.
    """

    code = "job_opening_not_accepting_candidates"
    default_message = _(
        "This job opening is not accepting new candidates. Only a published "
        "job opening can receive applications."
    )


class CandidateNotFound(RecruitmentError):
    """
    The candidate does not exist, or belongs to another company.

    Deliberately identical for both cases, matching JobOpeningNotFound: a
    distinct "exists but forbidden" answer would confirm the existence of
    another tenant's candidate to anyone able to guess an id.
    """

    code = "candidate_not_found"
    default_message = _("No candidate found matching the query.")


class InvalidStageTransition(RecruitmentError):
    """
    The requested stage move is not allowed for this candidate.

    Covers a stage belonging to a different job opening, and moving a candidate
    who has no pipeline at all.
    """

    code = "invalid_stage_transition"
    default_message = _("This candidate cannot be moved to that stage.")


class StageMoveNotPermitted(RecruitmentError):
    """
    The user may act on this candidate, but not make THIS move.

    Distinct from RecruitmentPermissionDenied (which means "not your
    candidate") and from InvalidStageTransition (which means "not a real
    stage of this pipeline"). This one is specifically about direction and
    role: a Stage Manager may only push a candidate forward out of their own
    stage, while a drive Manager may move in any direction. Keeping it separate
    lets the UI explain which rule was hit instead of showing a bare refusal.
    """

    code = "stage_move_not_permitted"
    default_message = _("You cannot move this candidate to that stage.")


class HiringHandoffRequired(RecruitmentError):
    """
    Someone tried to put a candidate in the Hired stage directly.

    Hiring is not a stage move: the PRD's Form 2 collects the designation,
    offered CTC, joining date and the handoff questions at the Final HR Round
    -> Hired boundary, and those are the record Onboarding consumes. A plain
    drag onto Hired would hire the candidate with none of it, so the move is
    refused and the user is sent to the handoff form, which performs the hire
    itself once it is complete.
    """

    code = "hiring_handoff_required"
    default_message = _(
        "Complete the Hiring Handoff form to hire this candidate. It collects "
        "the designation, offered CTC and joining date, and moves them to "
        "Hired once submitted."
    )


class StageRemarkRequired(RecruitmentError):
    """
    A single-candidate stage move was attempted without a remark.

    The PRD requires a remark on every Move Forward and every Reject: the
    remark is the accountability record for why a person advanced or stopped.
    Bulk actions are the deliberate exception and do not raise this.
    """

    code = "stage_remark_required"
    default_message = _("Enter a remark explaining this decision.")


class CandidateCompanyUndeterminable(RecruitmentError):
    """
    A candidate was created with no company that could be derived.

    A candidate with no company is visible to every tenant (the company manager
    passes NULL through), so creation is refused rather than producing an
    unscoped row.
    """

    code = "candidate_company_undeterminable"
    default_message = _(
        "The company for this candidate could not be determined. Select a "
        "company, or create the candidate from a job opening."
    )


class ContactVerificationError(RecruitmentError):
    """
    A verification step could not be completed.

    Covers an unknown or already-used token, a lapsed 10-minute window, and a
    wrong OTP. Deliberately one error: distinguishing "wrong code" from
    "expired" for an anonymous caller only helps someone probing tokens.
    """

    code = "contact_verification_failed"
    default_message = _(
        "This verification link or code is no longer valid. Start verification "
        "again to receive a new one."
    )


class CandidateDocumentInvalid(RecruitmentError):
    """An uploaded candidate document failed validation (type or size)."""

    code = "candidate_document_invalid"
    default_message = _("The uploaded document is not a valid PDF within the size limit.")


class GoogleCalendarNotConnected(RecruitmentError):
    """The scheduler has not connected their Google account to Krew."""

    code = "google_calendar_not_connected"
    default_message = _(
        "Connect your Google account to Krew so the meeting link can be fetched."
    )


class GoogleCalendarUnavailable(RecruitmentError):
    """Google is not set up for the company, or the calendar could not be read."""

    code = "google_calendar_unavailable"
    default_message = _(
        "Google Calendar is not set up for this company. An admin adds it in "
        "Settings > Google Meet."
    )
