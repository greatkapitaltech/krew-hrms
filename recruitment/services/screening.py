"""
Screening questions: the publication snapshot, and candidate answers.

Publication is the only freeze point:

    Question/Template -> PUBLISH -> immutable JobOpeningQuestion -> CandidateAnswer

After publication nothing in the reusable question/template system can change a
published opening's questions or the meaning of an answer already given, which
is why the snapshot copies wording, type, options, mandatory and order rather
than referencing them -- a reference would still drift.
"""

import logging

from django.db import transaction
from django.utils.translation import gettext_lazy as _

from recruitment.services.audit import RecruitmentAuditService
from recruitment.services.errors import (
    RecruitmentError,
    ScreeningAnswerValidationError,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Snapshot construction
# ---------------------------------------------------------------------------


def _question_sort_key(question):
    """
    Deterministic order for snapshot construction.

    ``sequence`` is nullable on both RecruitmentSurvey and
    SurveyTemplateQuestion, and NULL has no defined position relative to an
    integer, so it is normalised to sort last. The primary key breaks ties, so
    two questions sharing a sequence always snapshot in the same order --
    never in whatever order the database happened to return rows.
    """
    sequence, pk = question
    return (sequence is None, sequence if sequence is not None else 0, pk)


def collect_questions_for_publication(job_opening, form_type=None):
    """
    The questions a job opening publishes, as an ordered, deduplicated list.

    Both existing paths are honoured, per the PRD:

        Recruitment.survey_templates  -> SurveyTemplateQuestion -> question
        RecruitmentSurvey.recruitment_ids                       -> question

    The union is deduplicated by SOURCE QUESTION, so a question reachable
    through a template *and* attached directly to the opening produces exactly
    one snapshot row -- never two.

    Mandatory resolution, in priority order:

    1. any template row for this opening that marks it mandatory wins
       (mandatory-wins). A question that is mandatory in *any* template applied
       to this opening is mandatory for this opening: it is the safe direction,
       since under-collecting a required answer cannot be repaired later, and
       it does not depend on which template the database returned first.
    2. otherwise, if it arrives only through templates, the template value
       (False at this point, given rule 1).
    3. otherwise -- the direct recruitment_ids path, where no template row
       exists to carry configuration -- RecruitmentSurvey.is_mandatory.

    A question whose templates disagree is logged at INFO with both values, so
    a surprising snapshot can be explained afterwards rather than silently
    hidden. The resolved value is what gets frozen.

    Returns a list of dicts ready to become JobOpeningQuestion rows.
    """
    from recruitment.models import FORM_ONE, RecruitmentSurvey, SurveyTemplateQuestion

    form_type = form_type or FORM_ONE

    # --- template path: one query, no N+1 over templates ---------------
    template_rows = SurveyTemplateQuestion.objects.filter(
        surveytemplate__in=job_opening.survey_templates.filter(form_type=form_type)
    ).select_related("recruitmentsurvey")

    collected = {}
    conflicts = {}

    for row in template_rows:
        question = row.recruitmentsurvey
        existing = collected.get(question.pk)
        if existing is None:
            collected[question.pk] = {
                "question": question,
                "is_mandatory": row.is_mandatory,
                "sequence": row.sequence,
                # Frozen when the question joined this template, so a later
                # bank edit cannot change what this template publishes.
                "frozen": row.frozen(),
            }
            continue

        # Same question via a second template.
        if existing["is_mandatory"] != row.is_mandatory:
            conflicts.setdefault(question.pk, set()).update(
                {existing["is_mandatory"], row.is_mandatory}
            )
        # mandatory-wins (documented rule 1)
        existing["is_mandatory"] = existing["is_mandatory"] or row.is_mandatory
        # Keep the earliest configured position among the templates, with the
        # same nulls-last normalisation used for the final sort.
        existing["sequence"] = min(
            [existing["sequence"], row.sequence],
            key=lambda value: (value is None, value if value is not None else 0),
        )

    # --- direct path: questions attached straight to the opening -------
    direct_questions = RecruitmentSurvey.objects.entire().filter(
        recruitment_ids=job_opening, form_type=form_type
    )
    for question in direct_questions:
        existing = collected.get(question.pk)
        if existing is None:
            # No template row exists, so the question's own value is the only
            # configuration available -- this is the documented fallback.
            collected[question.pk] = {
                "question": question,
                "is_mandatory": question.is_mandatory,
                "sequence": question.sequence,
                "frozen": {
                    "wording": question.question,
                    "question_type": question.type,
                    "options": question.options or "",
                    "max_files": max(1, question.max_files or 1),
                    "allow_multiple_files": question.allow_multiple_files,
                },
            }
        else:
            # Reachable both ways: already collected. Deliberately do NOT add a
            # second entry -- this is the deduplication requirement. The
            # template configuration stays authoritative, except that a
            # mandatory direct question still forces mandatory (rule 1).
            if question.is_mandatory and not existing["is_mandatory"]:
                conflicts.setdefault(question.pk, set()).update({True, False})
                existing["is_mandatory"] = True

    for question_pk, values in conflicts.items():
        logger.info(
            "Job opening %s: question %s has conflicting mandatory settings %s "
            "across the templates/paths applied to it; snapshotting as "
            "mandatory=True (mandatory-wins).",
            job_opening.pk,
            question_pk,
            sorted(values),
        )

    ordered = sorted(
        collected.values(),
        key=lambda entry: _question_sort_key(
            (entry["sequence"], entry["question"].pk)
        ),
    )

    return [
        {
            "source_question": entry["question"],
            "wording": entry["frozen"]["wording"],
            "question_type": entry["frozen"]["question_type"],
            "options": entry["frozen"]["options"],
            "is_mandatory": bool(entry["is_mandatory"]),
            "display_order": index,
            "form_type": form_type,
            # The file cap is only meaningful on a file question; every other
            # type takes exactly one answer. `.get()` because a reconstructed
            # frozen dict may predate these keys.
            "max_files": (
                max(1, entry["frozen"].get("max_files") or 1)
                if entry["frozen"]["question_type"] == "file"
                else 1
            ),
            # Several files require BOTH a file question and its author's
            # consent. This was previously derived from the question type
            # alone, which made every file question multi-file and left the
            # single-file handling in submit_answers unreachable.
            "allow_multiple_files": (
                entry["frozen"]["question_type"] == "file"
                and max(1, entry["frozen"].get("max_files") or 1) > 1
            ),
        }
        for index, entry in enumerate(ordered)
    ]


def build_snapshot(job_opening, *, actor=None, reconstructed=False, form_type=None):
    """
    Freeze this job opening's screening questions. Idempotent.

    MUST be called inside the publish transaction (see
    recruitment.services.job_opening._transition), so that a failure here
    rolls the whole publication back: no PUBLISHED status, no partial
    snapshot, and no successful publication audit event.

    Idempotency: if any snapshot row already exists for the opening, the
    existing set is returned untouched. A retried or duplicated publish can
    therefore never produce a second set of rows, and can never rewrite the
    first. The unique(job_opening, source_question) constraint enforces the
    same thing at the database level.

    Returns the list of JobOpeningQuestion rows for the opening.
    """
    from recruitment.models import FORM_ONE, JobOpeningQuestion, RecruitmentAuditEvent

    form_type = form_type or FORM_ONE

    # Per form: freezing Form 1 at publication must not block Form 2 from
    # being frozen later at the hiring handoff.
    existing = list(
        JobOpeningQuestion.objects.entire()
        .filter(job_opening=job_opening, form_type=form_type)
        .order_by("display_order", "id")
    )
    if existing:
        logger.info(
            "Job opening %s already has %s %s question snapshot(s); leaving "
            "them unchanged (idempotent).",
            job_opening.pk,
            len(existing),
            form_type,
        )
        return existing

    specs = collect_questions_for_publication(job_opening, form_type)
    if not specs:
        # A job opening with no screening questions is legitimate -- the PRD
        # does not require any. Record nothing and let publication proceed.
        return []

    rows = JobOpeningQuestion.objects.bulk_create(
        [
            JobOpeningQuestion(
                job_opening=job_opening,
                is_reconstructed=reconstructed,
                **spec,
            )
            for spec in specs
        ]
    )

    RecruitmentAuditService.record(
        event_type=RecruitmentAuditEvent.EventType.JOB_OPENING_QUESTIONS_SNAPSHOTTED,
        actor=actor,
        job_opening=job_opening,
        details={
            "question_count": len(rows),
            "reconstructed": reconstructed,
            "form_type": form_type,
            # Source question ids only -- enough to trace provenance without
            # copying candidate-facing content into the audit trail.
            "source_question_ids": [
                spec["source_question"].pk for spec in specs
            ],
        },
    )
    return rows


def resolved_questions_for_display(job_opening, form_type=None):
    """
    One form's question set, annotated with where each question came from.

    Read-only, for the Job Opening configuration and review screens: it answers
    "exactly what will candidates see / what will HR be asked at handoff, and
    which of it came from the reusable template versus this opening alone".

    Before publication the set is what publication WOULD freeze; afterwards it
    is the frozen snapshot itself, so the review screen keeps showing the real
    question set once the opening is live.

    Deliberately NOT a "source" key on the collect_questions_for_publication()
    specs: those dicts are splatted straight into JobOpeningQuestion(**spec) by
    build_snapshot(), so an extra key there would break publication.

    ``source`` is "job_opening" when the question is attached directly to this
    opening and reaches no selected template of this form -- that is exactly
    the additive, opening-only case -- and "template" otherwise.
    """
    from recruitment.models import FORM_ONE, RecruitmentSurvey, SurveyTemplateQuestion

    form_type = form_type or FORM_ONE
    type_labels = dict(RecruitmentSurvey.question_types)

    template_question_ids = set(
        SurveyTemplateQuestion.objects.filter(
            surveytemplate__in=job_opening.survey_templates.filter(
                form_type=form_type
            )
        ).values_list("recruitmentsurvey_id", flat=True)
    )

    def _row(
        wording, question_type, is_mandatory, max_files, source_id, frozen
    ):
        cap = max(1, max_files or 1)
        return {
            "wording": wording,
            "question_type": question_type,
            "question_type_display": type_labels.get(question_type, question_type),
            "is_mandatory": is_mandatory,
            # Only meaningful for a file question; the UI shows a dash otherwise.
            "max_files": cap,
            "allow_multiple_files": cap > 1,
            "is_file": question_type == "file",
            "source": "template" if source_id in template_question_ids else "job_opening",
            "frozen": frozen,
        }

    snapshot = list(published_questions(job_opening, form_type))
    if snapshot:
        return [
            _row(
                row.wording,
                row.question_type,
                row.is_mandatory,
                row.max_files,
                row.source_question_id,
                True,
            )
            for row in snapshot
        ]

    return [
        _row(
            spec["wording"],
            spec["question_type"],
            spec["is_mandatory"],
            spec["max_files"],
            spec["source_question"].pk,
            False,
        )
        for spec in collect_questions_for_publication(job_opening, form_type)
    ]


def _ordered_with_sequence(job_opening, form_type):
    """The opening's questions in display order, each with its sort sequence."""
    from django.db.models import Min

    from recruitment.models import SurveyTemplateQuestion

    templates = job_opening.survey_templates.filter(form_type=form_type)
    rows = []
    for entry in collect_questions_for_publication(job_opening, form_type):
        question = entry["source_question"]
        sequence = (
            SurveyTemplateQuestion.objects.filter(
                surveytemplate__in=templates, recruitmentsurvey=question
            ).aggregate(seq=Min("sequence"))["seq"]
        )
        if sequence is None:
            sequence = question.sequence or 0
        rows.append((question, entry["wording"], sequence))
    return rows


def position_choices(job_opening, form_type):
    """
    Where a job-specific question can go (PRD: "anywhere in the order"):
    at the end, at the start, or after any question already in the set.
    """
    choices = [("end", _("At the end")), ("start", _("At the start"))]
    for question, wording, _sequence in _ordered_with_sequence(job_opening, form_type):
        choices.append((f"after:{question.pk}", _("After: %(q)s") % {"q": wording[:80]}))
    return choices


def sequence_for_position(job_opening, form_type, position):
    """
    The sequence that places a NEW job-specific question at ``position``.

    Questions are ordered by (sequence, question id). Template questions are
    numbered 1..N per template, so a new question given the same sequence as
    the one it follows sorts right after it (its id is newer).
    """
    rows = _ordered_with_sequence(job_opening, form_type)
    sequences = [sequence for _q, _w, sequence in rows]
    if position == "start":
        return min(sequences + [1]) - 1
    if position and position.startswith("after:"):
        target = position.split(":", 1)[1]
        for question, _wording, sequence in rows:
            if str(question.pk) == target:
                return sequence
    return max(sequences + [0]) + 1


def question_sections(job_opening):
    """
    Both forms' resolved question sets, ready for a template loop.

    Shared by the Job Opening configuration screen and the publication review
    screen so the two always describe the same thing.
    """
    from recruitment.models import FORM_ONE, FORM_TWO

    return [
        {
            "title": _("Screening Questions — Form 1"),
            "subtitle": _("Asked on the public application form."),
            "form_type": FORM_ONE,
            "templates": list(job_opening.survey_templates.filter(form_type=FORM_ONE)),
            "questions": resolved_questions_for_display(job_opening, FORM_ONE),
        },
        {
            "title": _("Hiring Handoff — Form 2"),
            "subtitle": _("Asked at the Final HR Round handoff, never publicly."),
            "form_type": FORM_TWO,
            "templates": list(job_opening.survey_templates.filter(form_type=FORM_TWO)),
            "questions": resolved_questions_for_display(job_opening, FORM_TWO),
        },
    ]


def published_questions(job_opening, form_type=None):
    """
    The frozen question set for a published job opening, in display order.

    This is the ONLY thing a candidate form, a validation pass or a report may
    read for a published opening. Reading the reusable question bank instead is
    what the snapshot exists to prevent.
    """
    from recruitment.models import FORM_ONE, JobOpeningQuestion

    return (
        JobOpeningQuestion.objects.entire()
        .filter(job_opening=job_opening, form_type=form_type or FORM_ONE)
        .order_by("display_order", "id")
    )


# ---------------------------------------------------------------------------
# Candidate answers
# ---------------------------------------------------------------------------

#: Input name prefix per question type, mirroring the legacy blob keys so the
#: rendered form and the parser cannot drift apart. The question id -- never
#: the wording -- identifies the field.
FIELD_PREFIX = {
    "multiple": "multiple_choices_",
    "date": "date_",
    "percentage": "percentage_",
    "file": "file_",
    "rating": "rating_",
}


def field_name_for(question):
    """
    The form field name for one snapshot question.

    Keyed on the snapshot's primary key, so renaming the reusable question --
    or two questions sharing wording -- cannot mis-route an answer. The legacy
    form used the wording itself as the field name, which is exactly the defect
    Feature 2 removes.
    """
    return f"{FIELD_PREFIX.get(question.question_type, '')}question_{question.pk}"


def submitted_field_name(question, data, files):
    """
    The name THIS submission actually used for one snapshot question.

    Normally ``field_name_for(question)`` -- keyed on the snapshot row. But a
    form can legitimately be rendered before the snapshot exists: Form 2 is
    frozen when the hiring handoff is submitted, so the handoff page the user
    filled in was rendered from the preview path, whose field names are keyed
    on the SOURCE question instead. By the time the answers are read the
    snapshot has just been created with different primary keys, so the posted
    names would match nothing and every answer -- including mandatory ones --
    would read as missing. That made the first handoff for such an opening
    impossible to submit.

    So the source-question name is accepted as a fallback, and only when the
    snapshot name is genuinely absent: the snapshot always wins, which keeps
    the normal published path (where both exist) unchanged.
    """
    name = field_name_for(question)
    if name in data or name in files:
        return name

    source_id = getattr(question, "source_question_id", None)
    if source_id:
        prefix = FIELD_PREFIX.get(question.question_type, "")
        fallback = f"{prefix}question_{source_id}"
        if fallback in data or fallback in files:
            return fallback
    return name


def _extract_answer(question, data, files):
    """
    Pull one question's answer out of the submission.

    Returns ``(present, text, uploaded)``. ``present`` distinguishes "the field
    was submitted and is empty" from "the field was not submitted at all" --
    without that distinction a partial re-submission would blank answers the
    candidate never touched.

    For a file question ``uploaded`` is a list when the question allows several
    files, and a single file (or None) otherwise.
    """
    name = submitted_field_name(question, data, files)
    present = name in data or name in files

    if question.question_type == "file":
        if question.allow_multiple_files:
            uploads = files.getlist(name) if hasattr(files, "getlist") else []
            if not uploads:
                single = files.get(name)
                uploads = [single] if single else []
            return (present, "", [f for f in uploads if f])
        return (present, "", files.get(name) or None)

    if question.question_type == "multiple":
        values = data.getlist(name) if hasattr(data, "getlist") else data.get(name) or []
        if isinstance(values, str):
            values = [values]
        # Comma-joined, matching how the previous blob stored multi-selects, so
        # display and export need no per-type special casing.
        return (present, ", ".join(v for v in values if v), None)

    return (present, str(data.get(name) or "").strip(), None)


def _store_question_documents(candidate, question, uploads):
    """
    Persist a file-upload answer as CandidateDocument rows for THIS application.

    Each file is validated independently (real PDF, 15 MB) by
    CandidateDocument.clean(), which is the same rule Feature 3 applies to every
    other candidate document. Documents are created against this candidate, so
    an earlier application by the same person is never reused.
    """
    from django.core.exceptions import ValidationError

    from recruitment.models import CandidateDocument
    from recruitment.services.errors import CandidateDocumentInvalid

    stored = []
    for upload in uploads:
        document = CandidateDocument(
            candidate_id=candidate,
            job_opening_question=question,
            title=(upload.name or "Document")[:250],
            document=upload,
            status="approved",
            document_type="handoff" if question.form_type == "form2" else "application",
        )
        try:
            document.full_clean(exclude=["document_request_id"])
        except ValidationError as error:
            # Converted to a RecruitmentError, the same way Feature 3's
            # upload_document() does it. This path is reached from the PUBLIC
            # application form, where an uncaught Django ValidationError would
            # surface to the applicant as a 500 with a traceback -- the caller
            # only handles RecruitmentError.
            raise CandidateDocumentInvalid(
                _("%(file)s: %(reason)s")
                % {
                    "file": upload.name or _("Document"),
                    "reason": " ".join(error.messages),
                }
            )
        document.save()
        stored.append(upload.name)
    return stored


def submit_answers(candidate, data, files=None, *, actor=None, form_type=None):
    """
    Persist a candidate's screening answers as one row per question.

    Validation uses the SNAPSHOT's is_mandatory, never the current
    RecruitmentSurvey value: a question that was mandatory when the opening was
    published stays mandatory for that opening even if the template is relaxed
    afterwards.

    Server-side enforcement matters here beyond correctness -- the previous
    implementation expressed "mandatory" only as an HTML ``required``
    attribute and wrote whatever was posted, so every mandatory question could
    be skipped by posting directly.

    Re-submission updates the existing row for that question rather than
    inserting a second one; unique(candidate, job_opening_question) enforces
    that in the database too.

    Raises ScreeningAnswerValidationError listing every missing mandatory
    question, so the candidate sees all of them at once.
    """
    from recruitment.models import CandidateAnswer, RecruitmentAuditEvent

    files = files or {}
    job_opening = candidate.recruitment_id
    if job_opening is None:
        raise RecruitmentError(
            _("This application is not linked to a job opening.")
        )

    # Scoped to one form: Form 2's answers are validated against Form 2's
    # frozen questions, never Form 1's. Defaults to Form 1, which is what
    # every caller before the hiring handoff means.
    questions = list(published_questions(job_opening, form_type))
    if not questions:
        return []

    with transaction.atomic():
        # Existing answers are read BEFORE validation: a re-submission must not
        # be forced to re-upload a file that is already on file, and a field
        # absent from this submission must keep whatever was stored.
        existing = {
            answer.job_opening_question_id: answer
            for answer in CandidateAnswer.objects.entire().filter(
                candidate=candidate, job_opening_question__in=questions
            )
        }
        was_update = bool(existing)

        parsed = {}
        missing = []
        for question in questions:
            present, text, uploaded = _extract_answer(question, data, files)
            prior = existing.get(question.pk)
            answered_before = bool(prior and (prior.answer or prior.attachment))

            if not present and prior is not None:
                # Not submitted this time: leave the stored answer untouched
                # rather than overwriting it with an empty string.
                continue

            # Each file question carries its own cap, frozen at publication.
            # Enforced here as well as in the browser: the counter on the form
            # is a courtesy, this is the rule.
            if question.question_type == "file" and hasattr(files, "getlist"):
                cap = max(1, question.max_files or 1)
                submitted = len(
                    files.getlist(submitted_field_name(question, data, files))
                )
                if submitted > cap:
                    if cap == 1:
                        raise ScreeningAnswerValidationError(
                            _("%(question)s accepts a single file.")
                            % {"question": question.wording}
                        )
                    raise ScreeningAnswerValidationError(
                        _("%(question)s accepts at most %(cap)s files.")
                        % {"question": question.wording, "cap": cap}
                    )

            has_upload = bool(uploaded) if isinstance(uploaded, list) else uploaded is not None
            if (
                question.is_mandatory
                and not text
                and not has_upload
                and not answered_before
            ):
                missing.append(question.wording)
            parsed[question.pk] = (question, text, uploaded)

        if missing:
            # Raised before any write, so the transaction rolls back with
            # nothing persisted and no audit event for a failed submission.
            raise ScreeningAnswerValidationError(missing_questions=missing)

        for question_pk, (question, text, uploaded) in parsed.items():
            answer = existing.get(question_pk)
            if answer is None:
                answer = CandidateAnswer(
                    candidate=candidate, job_opening_question=question
                )
            answer.answer = text
            if isinstance(uploaded, list):
                # Several files for one question become CandidateDocument rows
                # so they live in the candidate's document store rather than
                # being squeezed into the single attachment field.
                names = _store_question_documents(candidate, question, uploaded)
                answer.answer = ", ".join(names)
            elif uploaded is not None:
                # A single file goes to the document store too (PRD: one
                # source of truth for documents, shown in the Documents tab
                # and covered by retention). Was: answer.attachment = uploaded
                names = _store_question_documents(candidate, question, [uploaded])
                answer.answer = ", ".join(names)
            answer.save()

        RecruitmentAuditService.record(
            event_type=(
                RecruitmentAuditEvent.EventType.CANDIDATE_ANSWER_UPDATED
                if was_update
                else RecruitmentAuditEvent.EventType.CANDIDATE_ANSWER_SUBMITTED
            ),
            actor=actor,
            job_opening=job_opening,
            candidate=candidate,
            # Counts and question ids only -- never the answer content, which
            # can carry personal information.
            details={
                "answer_count": len(parsed),
                "job_opening_question_ids": sorted(parsed.keys()),
            },
        )

    return list(
        CandidateAnswer.objects.entire()
        .filter(candidate=candidate)
        .select_related("job_opening_question")
        .order_by("job_opening_question__display_order")
    )


def answers_for_candidate(candidate, form_type=None):
    """
    A candidate's answers with their frozen questions, in display order.
    ``form_type`` narrows to one form (Form 1 application / Form 2 handoff).

    select_related on the snapshot keeps display and export at two queries
    regardless of how many questions there are -- the previous implementation
    re-read and re-parsed the JSON blob per question.
    """
    from recruitment.models import CandidateAnswer

    answers = CandidateAnswer.objects.entire().filter(candidate=candidate)
    if form_type:
        answers = answers.filter(job_opening_question__form_type=form_type)
    return answers.select_related("job_opening_question").order_by(
        "job_opening_question__display_order", "id"
    )
