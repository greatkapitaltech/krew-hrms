"""
recruitment/accessibility.py

"""

from recruitment.templatetags.recruitmentfilters import recruitment_manages


def add_candidate_accessibility(
    request, instance=None, user_perms=[], *args, **kwargs
) -> bool:
    """
    Candidate add accessibility. Never into the Rejected column.
    """
    if instance is not None and instance.is_rejected_stage:
        return False
    return (
        request.user.has_perm("recruitment.add_candidate")
        or request.user.employee_get in instance.stage_managers.all()
        or request.user.employee_get
        in instance.recruitment_id.recruitment_managers.all()
    )


def edit_stage_accessibility(
    request, instance=None, user_perms=[], *args, **kwargs
) -> bool:
    """
    Edit stage accessibility. Fixed stages are never edited (PRD).
    """
    if instance is not None and instance.is_fixed:
        return False
    return (
        request.user.has_perm("recruitment.change_stage")
        or recruitment_manages(request.user, instance.recruitment_id)
        or request.user.employee_get in instance.stage_managers.all()
    )


def delete_stage_accessibility(
    request, instance=None, user_perms=[], *args, **kwargs
) -> bool:
    """
    Delete stage accessibility. Fixed stages are never removed (PRD).
    """
    if instance is not None and instance.is_fixed:
        return False
    return request.user.has_perm("recruitment.delete_stage")


def edit_fixed_stage_managers_accessibility(
    request, instance=None, user_perms=[], *args, **kwargs
) -> bool:
    """"Edit Managers" shows on fixed stages only, where Edit is hidden."""
    return (
        instance is not None
        and instance.is_fixed
        and not instance.is_rejected_stage
        and (
            request.user.has_perm("recruitment.change_stage")
            or recruitment_manages(request.user, instance.recruitment_id)
        )
    )
