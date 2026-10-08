"""
Career Page: the company's public job link, its iframe embed code, and the
career sites allowed to embed it.
"""

from django.contrib import messages
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from horilla.decorators import hx_request_required, login_required, permission_required
from recruitment.models import RecruitmentGeneralSetting
from recruitment.services.authorization import selectable_companies_for_user
from recruitment.services.job_opening import career_page_slug, normalize_career_origin


@login_required
@hx_request_required
@permission_required("recruitment.change_recruitment")
def career_page(request):
    from recruitment.models import Recruitment

    companies = list(selectable_companies_for_user(request.user).order_by("company"))
    # Opened from a job opening row: show that job's own apply link too.
    opening = None
    opening_id = request.GET.get("opening") or request.POST.get("opening")
    if opening_id and str(opening_id).isdigit():
        opening = (
            Recruitment.objects.filter(
                pk=opening_id, company_id__in=[c.pk for c in companies]
            )
            .select_related("company_id")
            .first()
        )
    chosen = request.POST.get("company") or request.GET.get("company")
    if opening is not None and not chosen:
        chosen = opening.company_id_id
    company = next((c for c in companies if str(c.pk) == str(chosen)), None)
    if opening is not None and company is not None and opening.company_id_id != company.pk:
        opening = None
    if company is None:
        selected = request.session.get("selected_company")
        company = next((c for c in companies if str(c.pk) == str(selected)), None)
    if company is None and companies:
        company = companies[0]

    setting = None
    errors = []
    if company is not None:
        setting, _created = RecruitmentGeneralSetting.objects.entire().get_or_create(
            company_id=company
        )
        if request.method == "POST":
            origins = []
            for line in request.POST.get("domains", "").splitlines():
                if not line.strip():
                    continue
                try:
                    origin = normalize_career_origin(line)
                except ValueError:
                    errors.append(line.strip())
                    continue
                if origin not in origins:
                    origins.append(origin)
            if errors:
                messages.error(
                    request,
                    _("Not a valid site address (use e.g. https://careers.example.com): %(bad)s")
                    % {"bad": ", ".join(errors)},
                )
            else:
                setting.career_page_domains = "\n".join(origins)
                setting.save()
                messages.success(request, _("Allowed career sites saved."))

    listing_url = ""
    apply_url = ""
    if company is not None:
        slug = career_page_slug(company)
        listing_url = request.build_absolute_uri(reverse("career-page", args=[slug]))
        if opening is not None and opening.status == Recruitment.Status.PUBLISHED:
            apply_url = request.build_absolute_uri(
                reverse("career-apply", args=[slug, opening.pk])
            )
    return render(
        request,
        "cbv/recruitment/career_page.html",
        {
            "companies": companies,
            "company": company,
            "domains": request.POST.get("domains") if errors else (
                setting.career_page_domains if setting else ""
            ),
            "listing_url": listing_url,
            "opening": opening,
            "apply_url": apply_url,
        },
    )


@hx_request_required
def career_job_details(request, slug, rec_id):
    """
    Public "View" panel on a career page: /careers/<slug>/jobs/<id>/.
    Only that company's listed openings; served from Redis for visitors.
    """
    from django.core.cache import cache
    from django.http import Http404, HttpResponse

    from recruitment.services.job_opening import (
        PUBLIC_LISTING_TTL,
        public_listing_cache_key,
        public_opening_for_slug,
    )

    cache_key = public_listing_cache_key(f"details-{slug}-{int(rec_id)}")
    cached = cache.get(cache_key)
    if cached is not None:
        return HttpResponse(cached)
    opening = public_opening_for_slug(slug, rec_id)
    if opening is None:
        raise Http404("No job opening found.")
    response = render(
        request,
        "recruitment/recruitment_details.html",
        {"recruitment": opening, "career_slug": slug},
    )
    cache.set(cache_key, response.content, PUBLIC_LISTING_TTL)
    return response


def career_apply(request, slug, rec_id):
    """
    Public application form on a career page: /careers/<slug>/apply/<id>/.
    Checks the opening belongs to this company, then runs the normal
    application form (verification, questions, submit) for it.
    """
    from django.http import Http404

    from recruitment.services.job_opening import public_opening_for_slug
    from recruitment.views.surveys import application_form

    opening = public_opening_for_slug(slug, rec_id)
    if opening is None:
        raise Http404("No job opening found.")
    if opening.status != opening.Status.PUBLISHED:
        # Closed: still listed, but no new applications (PRD).
        from django.contrib import messages
        from django.shortcuts import redirect

        messages.info(
            request,
            _("%(title)s is no longer accepting applications.") % {"title": opening.title},
        )
        return redirect("career-page", slug=slug)
    request.GET = request.GET.copy()
    request.GET["recruitmentId"] = str(rec_id)
    request.krew_career_apply = True
    return application_form(request)

