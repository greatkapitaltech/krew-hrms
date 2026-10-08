from django.db.models.signals import m2m_changed, post_delete, post_save
from django.dispatch import receiver

from recruitment.models import (
    Candidate,
    STAGE_APPLIED,
    TERMINAL_STAGE_DEFAULTS,
    CandidateDocument,
    CandidateDocumentRequest,
    Recruitment,
    RecruitmentGeneralSetting,
    Stage,
)


@receiver(post_save, sender=Recruitment)
def create_initial_stage(sender, instance, created, **kwargs):
    """
    Seed the fixed pipeline stages for a new job opening.

    The PRD's pipeline is Applied -> custom stages -> Final HR Round -> Hired,
    so exactly three stages are seeded: the entry stage every application lands
    in, and the terminal pair Feature 4 needs -- the hiring handoff is only
    offered at Final HR Round, and hire_candidate() moves the candidate into
    the Hired stage. Everything between the two is the client's own, added from
    the pipeline screen.

    Upstream also seeded an "Initial" stage here. It is deliberately not
    seeded: the PRD gives a new opening three stages, and Applied already is
    the entry stage (see services.candidate.entry_stage). The stage_type itself
    stays in Stage.stage_types, so an existing Initial stage a client still
    uses keeps working.

    Every stage is matched on stage_type, never on name. Stage.Meta
    .unique_together is ("recruitment_id", "stage"), so a name-keyed check
    would happily create a second Hired stage under a different name, and
    nothing in the schema forbids two stages sharing a type.
    """
    if not created:
        return

    for stage_type, name, sequence in (
        (STAGE_APPLIED, "Applied", 0),
    ) + TERMINAL_STAGE_DEFAULTS:
        Stage.objects.get_or_create(
            recruitment_id=instance,
            stage_type=stage_type,
            defaults={"stage": name, "sequence": sequence},
        )


@receiver(m2m_changed, sender=Recruitment.recruitment_managers.through)
def assign_drive_managers_to_fixed_stages(sender, instance, action, **kwargs):
    """
    Give the fixed stages (Applied, Final HR Round, Hired) a manager.

    The PRD allows no stage to go live without a Stage Manager, and the fixed
    stages are seeded empty. When drive Managers are set on an opening, they
    are added to every fixed stage that still has none -- a stage whose
    managers someone already chose is left alone.
    """
    if action != "post_add" or not isinstance(instance, Recruitment):
        return
    managers = list(instance.recruitment_managers.all())
    if not managers:
        return
    fixed = Stage.objects.entire().filter(
        recruitment_id=instance,
        stage_type__in=[STAGE_APPLIED] + [t for t, _n, _s in TERMINAL_STAGE_DEFAULTS],
    )
    for stage in fixed:
        if not stage.stage_managers.exists():
            stage.stage_managers.add(*managers)


@receiver(m2m_changed, sender=CandidateDocumentRequest.candidate_id.through)
def document_request_m2m_changed(sender, instance, action, **kwargs):
    if action == "post_add":
        candidate_document_create(instance)

    elif action == "post_remove":
        candidate_document_create(instance)


def candidate_document_create(instance):
    candidates = instance.candidate_id.all()
    for candidate in candidates:
        document, created = CandidateDocument.objects.get_or_create(
            candidate_id=candidate,
            document_request_id=instance,
            defaults={"title": f"Upload {instance.title}"},
        )
        document.title = f"Upload {instance.title}"
        document.save()


@receiver(post_save, sender=Candidate)
def keep_resume_in_documents(sender, instance, raw=False, **kwargs):
    """Every resume (new or replaced) becomes the active resume document."""
    if raw:
        return
    from recruitment.services.candidate import sync_resume_document

    sync_resume_document(instance)


@receiver(post_save, sender=Recruitment)
@receiver(post_delete, sender=Recruitment)
def refresh_public_job_listing(sender, instance, **kwargs):
    """Careers pages show the change at once (cached listing is retired)."""
    from recruitment.services.job_opening import invalidate_public_listing

    invalidate_public_listing()


@receiver(post_save, sender=RecruitmentGeneralSetting)
def refresh_career_page_domains(sender, instance, **kwargs):
    """New allowed domains apply to the next page load."""
    from recruitment.services.job_opening import invalidate_career_origins

    invalidate_career_origins(instance.company_id_id)

