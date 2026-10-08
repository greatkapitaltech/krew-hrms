"""
Audit trail for Recruitment set-up changes made outside the services layer.

Stages, the question bank, templates and the template / job-opening question
links are edited from several screens (including Horilla's generic delete), so
they are audited here, on the models, rather than in each view. Every change
writes one RecruitmentAuditEvent; updates record only the fields that actually
changed, as [old, new]. The acting user comes from the current request.
"""

from django.db.models.signals import m2m_changed, post_delete, post_save, pre_save
from django.dispatch import receiver

from recruitment.models import (
    Recruitment,
    RecruitmentAuditEvent,
    RecruitmentSurvey,
    Stage,
    SurveyTemplate,
    SurveyTemplateQuestion,
)
from recruitment.services.audit import RecruitmentAuditService

E = RecruitmentAuditEvent.EventType

STAGE_FIELDS = ("stage", "stage_type", "sequence")
QUESTION_FIELDS = ("question", "type", "options", "form_type", "max_files", "is_mandatory")
TEMPLATE_FIELDS = ("title", "form_type", "description")


def _remember(sender, instance, fields):
    """Keep the stored values so post_save can tell what changed."""
    if instance.pk:
        old = sender.objects.entire().filter(pk=instance.pk).values(*fields).first() \
            if hasattr(sender.objects, "entire") else \
            sender.objects.filter(pk=instance.pk).values(*fields).first()
        instance._audit_old = old or {}
    else:
        instance._audit_old = {}


def _changes(instance, fields):
    old = getattr(instance, "_audit_old", {}) or {}
    return {
        f: [str(old.get(f)), str(getattr(instance, f))]
        for f in fields
        if f in old and old.get(f) != getattr(instance, f)
    }


# ---- Stages -----------------------------------------------------------------
@receiver(pre_save, sender=Stage)
def _stage_pre(sender, instance, raw=False, **kwargs):
    if not raw:
        _remember(sender, instance, STAGE_FIELDS)


@receiver(post_save, sender=Stage)
def _stage_saved(sender, instance, created, raw=False, **kwargs):
    if raw:
        return
    if created:
        RecruitmentAuditService.record(
            event_type=E.STAGE_CREATED, job_opening=instance.recruitment_id, stage=instance,
            details={"stage": instance.stage, "stage_type": instance.stage_type,
                     "sequence": instance.sequence},
        )
        return
    changed = _changes(instance, STAGE_FIELDS)
    if changed:
        RecruitmentAuditService.record(
            event_type=E.STAGE_UPDATED, job_opening=instance.recruitment_id, stage=instance,
            details={"stage": instance.stage, "changes": changed},
        )


@receiver(post_delete, sender=Stage)
def _stage_deleted(sender, instance, **kwargs):
    opening = Recruitment.objects.entire().filter(pk=instance.recruitment_id_id).first()
    RecruitmentAuditService.record(
        event_type=E.STAGE_DELETED, job_opening=opening,
        object_type="Stage", object_id=instance.pk,
        details={"stage": instance.stage, "stage_type": instance.stage_type},
    )


@receiver(m2m_changed, sender=Stage.stage_managers.through)
def _stage_managers(sender, instance, action, pk_set, reverse=False, **kwargs):
    if reverse or action not in ("post_add", "post_remove", "post_clear"):
        return
    if action == "post_add" and not pk_set:
        return
    RecruitmentAuditService.record(
        event_type=E.STAGE_MANAGERS_CHANGED, job_opening=instance.recruitment_id, stage=instance,
        details={
            "stage": instance.stage,
            "action": action.replace("post_", ""),
            "employee_ids": sorted(pk_set or []),
            "managers_now": sorted(instance.stage_managers.values_list("pk", flat=True)),
        },
    )


# ---- Question bank ------------------------------------------------------------
@receiver(pre_save, sender=RecruitmentSurvey)
def _question_pre(sender, instance, raw=False, **kwargs):
    if not raw:
        _remember(sender, instance, QUESTION_FIELDS)


@receiver(post_save, sender=RecruitmentSurvey)
def _question_saved(sender, instance, created, raw=False, **kwargs):
    if raw:
        return
    if created:
        RecruitmentAuditService.record(
            event_type=E.QUESTION_CREATED, object_type="RecruitmentSurvey", object_id=instance.pk,
            details={"question": instance.question[:200], "type": instance.type,
                     "form_type": instance.form_type},
        )
        return
    changed = _changes(instance, QUESTION_FIELDS)
    if changed:
        RecruitmentAuditService.record(
            event_type=E.QUESTION_UPDATED, object_type="RecruitmentSurvey", object_id=instance.pk,
            details={"changes": changed},
        )


@receiver(post_delete, sender=RecruitmentSurvey)
def _question_deleted(sender, instance, **kwargs):
    RecruitmentAuditService.record(
        event_type=E.QUESTION_DELETED, object_type="RecruitmentSurvey", object_id=instance.pk,
        details={"question": instance.question[:200], "form_type": instance.form_type},
    )


@receiver(m2m_changed, sender=RecruitmentSurvey.recruitment_ids.through)
def _job_opening_questions(sender, instance, action, pk_set, reverse=False, **kwargs):
    """Extra (job-specific) questions attached to / detached from an opening."""
    if action not in ("post_add", "post_remove") or not pk_set:
        return
    event = E.JOB_OPENING_QUESTION_ADDED if action == "post_add" else E.JOB_OPENING_QUESTION_REMOVED
    if reverse:  # opening.recruitmentsurvey_set.add(questions)
        pairs = [(instance, q) for q in RecruitmentSurvey.objects.entire().filter(pk__in=pk_set)]
    else:        # question.recruitment_ids.add(openings)
        pairs = [(o, instance) for o in Recruitment.objects.entire().filter(pk__in=pk_set)]
    for opening, question in pairs:
        RecruitmentAuditService.record(
            event_type=event, job_opening=opening,
            details={"question_id": question.pk, "question": question.question[:200],
                     "form_type": question.form_type},
        )


# ---- Templates ----------------------------------------------------------------
@receiver(pre_save, sender=SurveyTemplate)
def _template_pre(sender, instance, raw=False, **kwargs):
    if not raw:
        _remember(sender, instance, TEMPLATE_FIELDS)


@receiver(post_save, sender=SurveyTemplate)
def _template_saved(sender, instance, created, raw=False, **kwargs):
    if raw:
        return
    if created:
        RecruitmentAuditService.record(
            event_type=E.TEMPLATE_CREATED, company=instance.company_id,
            object_type="SurveyTemplate", object_id=instance.pk,
            details={"template": instance.title, "form_type": instance.form_type},
        )
        return
    changed = _changes(instance, TEMPLATE_FIELDS)
    if changed:
        RecruitmentAuditService.record(
            event_type=E.TEMPLATE_UPDATED, company=instance.company_id,
            object_type="SurveyTemplate", object_id=instance.pk,
            details={"template": instance.title, "changes": changed},
        )


@receiver(post_delete, sender=SurveyTemplate)
def _template_deleted(sender, instance, **kwargs):
    RecruitmentAuditService.record(
        event_type=E.TEMPLATE_DELETED, company=instance.company_id,
        object_type="SurveyTemplate", object_id=instance.pk,
        details={"template": instance.title, "form_type": instance.form_type},
    )


# ---- Template <-> question rows ---------------------------------------------
@receiver(pre_save, sender=SurveyTemplateQuestion)
def _tq_pre(sender, instance, raw=False, **kwargs):
    if not raw:
        old = sender.objects.filter(pk=instance.pk).values("is_mandatory").first() if instance.pk else None
        instance._audit_old = old or {}


def _tq_details(row):
    return {"template": row.surveytemplate.title, "template_id": row.surveytemplate_id,
            "question_id": row.recruitmentsurvey_id, "question": (row.wording or "")[:200]}


@receiver(post_save, sender=SurveyTemplateQuestion)
def _tq_saved(sender, instance, created, raw=False, **kwargs):
    if raw:
        return
    template = instance.surveytemplate
    if created:
        RecruitmentAuditService.record(
            event_type=E.QUESTION_ADDED_TO_TEMPLATE, company=template.company_id,
            object_type="SurveyTemplate", object_id=template.pk, details=_tq_details(instance),
        )
        return
    old = getattr(instance, "_audit_old", {}) or {}
    if "is_mandatory" in old and old["is_mandatory"] != instance.is_mandatory:
        RecruitmentAuditService.record(
            event_type=E.QUESTION_MADE_MANDATORY if instance.is_mandatory else E.QUESTION_MADE_OPTIONAL,
            company=template.company_id, object_type="SurveyTemplate", object_id=template.pk,
            details=_tq_details(instance),
        )


@receiver(post_delete, sender=SurveyTemplateQuestion)
def _tq_deleted(sender, instance, **kwargs):
    template = SurveyTemplate.objects.entire().filter(pk=instance.surveytemplate_id).first() \
        if hasattr(SurveyTemplate.objects, "entire") else None
    RecruitmentAuditService.record(
        event_type=E.QUESTION_REMOVED_FROM_TEMPLATE,
        company=getattr(template, "company_id", None),
        object_type="SurveyTemplate", object_id=instance.surveytemplate_id,
        details={"template_id": instance.surveytemplate_id,
                 "question_id": instance.recruitmentsurvey_id,
                 "question": (instance.wording or "")[:200]},
    )


@receiver(m2m_changed, sender=RecruitmentSurvey.template_id.through)
def _tq_m2m(sender, instance, action, pk_set, reverse=False, **kwargs):
    """Template links made with .add()/.set()/.remove() (these skip save())."""
    if action not in ("post_add", "post_remove") or not pk_set:
        return
    event = E.QUESTION_ADDED_TO_TEMPLATE if action == "post_add" else E.QUESTION_REMOVED_FROM_TEMPLATE
    if reverse:
        pairs = [(instance, q) for q in RecruitmentSurvey.objects.entire().filter(pk__in=pk_set)]
    else:
        pairs = [(t, instance) for t in SurveyTemplate.objects.entire().filter(pk__in=pk_set)]
    for template, question in pairs:
        RecruitmentAuditService.record(
            event_type=event, company=template.company_id,
            object_type="SurveyTemplate", object_id=template.pk,
            details={"template": template.title, "template_id": template.pk,
                     "question_id": question.pk, "question": question.question[:200]},
        )
