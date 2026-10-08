"""
This module used for recruitment candidates
"""

import ast
import io
import json
import re
from typing import Any

from bs4 import BeautifulSoup
from django import forms
from django.contrib import messages
from django.db.models import Min
from django.http import HttpResponse
from django.shortcuts import render
from django.template.loader import render_to_string
from django.urls import reverse, reverse_lazy
from django.utils.decorators import method_decorator
from django.utils.translation import gettext_lazy as _
from django.views import View
from import_export import fields, resources
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from xhtml2pdf import pisa

from base.methods import has_export_access
from employee.forms import BulkUpdateFieldForm
from horilla.horilla_middlewares import _thread_locals
from horilla.http.response import HorillaRedirect
from horilla_views.cbv_methods import (
    export_xlsx,
    hx_request_required,
    login_required,
    permission_required,
)
from horilla_views.forms import DynamicBulkUpdateForm
from horilla_views.generic.cbv.views import (
    HorillaCardView,
    HorillaDetailedView,
    HorillaFormView,
    HorillaListView,
    HorillaNavView,
    TemplateView,
)
from horilla_views.templatetags.generic_template_filters import getattribute
from recruitment.cbv.candidate_reject_reason import DynamicRejectReasonFormView
from recruitment.cbv_decorators import all_manager_can_enter, manager_can_enter
from recruitment.filters import CandidateFilter
from recruitment.forms import (
    CandidateExportForm,
    RejectedCandidateForm,
    ToSkillZoneForm,
)
from recruitment.models import (
    Candidate,
    RecruitmentSurvey,
    RecruitmentSurveyAnswer,
    RejectedCandidate,
    SkillZoneCandidate,
)

_getattribute = getattribute


def clean_column_name(question):
    """
    Convert the question text into a safe attribute name by:
    - Replacing spaces with underscores
    - Removing special characters except underscores
    """
    return re.sub(r"[^\w\s]", "", question).replace(" ", "_")


@method_decorator(
    permission_required(perm="recruitment.view_candidate"), name="dispatch"
)
@method_decorator(login_required, name="dispatch")
class CandidatesView(TemplateView):
    """
    For page view

    """

    template_name = "cbv/candidates/candidates.html"

    def get_context_data(self, **kwargs: Any) -> dict:
        context = super().get_context_data(**kwargs)
        update_fields = BulkUpdateFieldForm()
        context["update_fields_form"] = update_fields
        return context


@method_decorator(login_required, name="dispatch")
@method_decorator(manager_can_enter(perm="recruitment.view_candidate"), name="dispatch")
class ListCandidates(HorillaListView):
    """
    List view of candidates
    """

    model = Candidate
    filter_class = CandidateFilter
    quick_export = False
    bulk_template = "cbv/employees_view/bulk_update_page.html"
    bulk_update_fields = [
        "gender",
        "job_position_id",
        "hired_date",
        "referral",
        "country",
        "state",
        "city",
        "zip",
        "joining_date",
        "probation_end",
    ]

    def get_bulk_form(self):
        """
        Bulk from generating method
        """

        form = DynamicBulkUpdateForm(
            root_model=Candidate, bulk_update_fields=self.bulk_update_fields
        )

        form.fields["country"] = forms.ChoiceField(
            required=False,
            widget=forms.Select(
                attrs={
                    "class": "oh-select oh-select-2",
                    "required": False,
                    "style": "width: 100%; height:45px;",
                }
            ),
        )

        form.fields["state"] = forms.ChoiceField(
            required=False,
            widget=forms.Select(
                attrs={
                    "class": "oh-select oh-select-2",
                    "required": False,
                    "style": "width: 100%; height:45px;",
                },
            ),
        )

        return form

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.export_fields = []
        self.search_url = reverse("list-candidate")
        if self.request.user.has_perm("recruitment.change_candidate"):
            self.option_method = "options"
        else:
            self.option_method = None
        self.action_method = "actions_col"

        # Exportable screening-question columns.
        #
        # Sourced from the published JobOpeningQuestion snapshots so the column
        # headings are the questions as they were frozen at publication, then
        # topped up from the reusable bank so a question that has never been
        # published still offers a column. Deduplicated by wording, because a
        # column is one heading regardless of how many openings froze it.
        from recruitment.models import JobOpeningQuestion

        self.survey_question_mapping = {}
        seen_wordings = set()

        snapshot_questions = (
            JobOpeningQuestion.objects.values("wording")
            .annotate(pk=Min("source_question"))
            .order_by("wording")
        )
        for question in snapshot_questions:
            wording = question["wording"]
            if question["pk"] is None or wording in seen_wordings:
                continue
            seen_wordings.add(wording)
            survey_question = (wording, f"get_survey_question_{question['pk']}")
            if survey_question not in self.export_fields:
                self.export_fields.append(survey_question)

        unique_questions = RecruitmentSurvey.objects.values("question").annotate(
            pk=Min("pk")
        )
        for question in unique_questions:
            if question["question"] in seen_wordings:
                continue
            seen_wordings.add(question["question"])
            survey_question = (
                question["question"],
                f"get_survey_question_{question['pk']}",
            )
            if not survey_question in self.export_fields:
                self.export_fields.append(survey_question)

    columns = [
        (_("Candidates"), "name", "get_avatar"),
        (_("Email"), "email"),
        (_("Phone"), "mobile"),
        (_("Rating"), "rating"),
        (_("Recruitment"), "recruitment_id"),
        (_("Job Position"), "job_position_id"),
        (_("Hired Date"), "hired_date"),
        (_("Resume"), "resume_pdf"),
    ]

    export_columns = [
        (_("Candidates"), "name"),
        (_("Email"), "email"),
        (_("Phone"), "mobile"),
        (_("Rating"), "get_avg_rating"),
        (_("Scheduled Interview"), "get_total_interview"),
        (_("Recruitment"), "recruitment_id"),
        (_("Job Position"), "job_position_id"),
        (_("Hired Date"), "hired_date"),
    ]
    default_columns = columns

    header_attrs = {
        "option": """
                   style ="width : 180px !important;"
                   """,
        "action": """
                   style ="width : 150px !important;"
                   """,
        "email": """
                   style ="width : 200px !important;"
                   """,
        "rating": """
                   style ="width : 170px !important;"
                   """,
    }

    sortby_mapping = [
        (_("Candidates"), "name", "get_avatar"),
    ]
    row_status_indications = [
        (
            "canceled--dot",
            _("Canceled"),
            """
            onclick="
                $('#applyFilter').closest('form').find('[name=canceled]').val('true');
                $('#applyFilter').click();
            "
            """,
        ),
        (
            "nothired--dot",
            _("Not Hired"),
            """
            onclick="
                $('#applyFilter').closest('form').find('[name=hired]').val('false');
                $('#applyFilter').click();
            "
            """,
        ),
        (
            "hired--dot",
            _("Hired"),
            """
            onclick="$('#applyFilter').closest('form').find('[name=hired]').val('true');
                $('#applyFilter').click();
            "
            """,
        ),
    ]

    row_status_class = "hired-{hired} canceled-{canceled}"

    # row_attrs = """
    #             {is_employee_converted}
    #             hx-get='{get_details_candidate}'
    #             data-toggle="oh-modal-toggle"
    #             data-target="#genericModal"
    #             hx-target="#genericModalBody"
    #             """
    row_attrs = """
                {is_employee_converted}
                hx-get="{get_profile_url}?instance_ids={ordered_ids}"
                hx-target="#listContainer"
                hx-swap="innerHTML"
                hx-push-url="{get_individual_url}"
                class="cursor-pointer"
                """

    def export_data(self, *args, **kwargs):
        """
        Export with survey answer and question
        """

        request = getattr(_thread_locals, "request", None)

        # Candidate export is bulk PII, so it requires the explicit
        # export_candidate permission. base.methods.has_export_access is
        # deliberately NOT used: it returns True for every user of a company
        # that has no DefaultExportPermission row configured, which is too
        # permissive for this data. Enforced here, server-side, not by hiding
        # the button.
        from recruitment.services import candidate as candidate_service
        from recruitment.services.errors import RecruitmentPermissionDenied

        try:
            candidate_service.assert_can_export(request.user)
        except RecruitmentPermissionDenied as error:
            return HttpResponse(str(error), status=403)

        ids = ast.literal_eval(request.POST["ids"])
        _columns = ast.literal_eval(request.POST["columns"])
        # Company-scoped: a supplied id belonging to another tenant is not
        # exported, so an IDOR through the id list cannot leak candidates.
        queryset = candidate_service.accessible_candidates(request.user).filter(
            id__in=ids
        )
        export_format_for_audit = request.POST.get("format", "xlsx")
        candidate_service.record_export(
            request.user,
            queryset,
            export_format=export_format_for_audit,
            filters={"selected_ids": len(ids)},
        )
        question_mapping = self.survey_question_mapping
        export_format = request.POST.get("format", "xlsx")

        _model = self.model

        class HorillaListViewResorce(resources.ModelResource):
            """
            Instant Resource class
            """

            id = fields.Field(column_name="ID")
            question = {}

            class Meta:
                """
                Meta class for additional option
                """

                model = _model
                fields = [field[1] for field in _columns]  # 773

            def __init__(self, **kwargs):
                super().__init__(**kwargs)

                for field_tuple in _columns:
                    if field_tuple[1].startswith("question_"):
                        safe_field_name = field_tuple[1]
                        self.fields[safe_field_name] = fields.Field(
                            column_name=question_mapping[safe_field_name],
                            attribute=safe_field_name,
                            readonly=True,
                        )

            def export_field(self, field, obj):
                """
                Override this method to fetch the candidate's answers dynamically.
                """

                if field.attribute:
                    # Get the stored JSON field containing answers
                    survey_answers = RecruitmentSurveyAnswer.objects.filter(
                        candidate_id=obj
                    ).first()
                    if survey_answers and field.attribute.startswith("question_"):
                        survey_answers = survey_answers.answer_json
                        if isinstance(survey_answers, str):
                            try:
                                survey_answers = ast.literal_eval(
                                    survey_answers
                                )  # Convert string to dict
                            except Exception:
                                survey_answers = {}

                        # Extract the actual question text

                        original_question = question_mapping[field.attribute]
                        # Retrieve answer from JSON if available
                        answer = survey_answers.get(original_question, "")
                        if not answer:
                            answer = survey_answers.get(
                                "rating_" + original_question, ""
                            )
                        if not answer:
                            answer = survey_answers.get(
                                "percentage_" + original_question, ""
                            )
                        if not answer:
                            answer = survey_answers.get("file_" + original_question, "")
                        if not answer:
                            answer = survey_answers.get("date_" + original_question, "")
                        if not answer:
                            answer = survey_answers.get(
                                "multiple_choices_" + original_question, ""
                            )
                        return answer

                return super().export_field(field, obj)

            def dehydrate_id(self, instance):
                """
                Dehydrate method for id field
                """
                return instance.pk

            def _make_dehydrate(field_name):
                # Factory, not exec(): closes over field_name by value so each
                # generated method reads its own column, without ever
                # compiling a string built from request data.
                def dehydrate(self, instance):
                    return self.remove_extra_spaces(getattribute(instance, field_name))

                return dehydrate

            for field_tuple in _columns:
                if not field_tuple[1].startswith("question_"):
                    locals()[f"dehydrate_{field_tuple[1]}"] = _make_dehydrate(
                        field_tuple[1]
                    )
                    locals()[field_tuple[1]] = fields.Field(column_name=field_tuple[0])

            def remove_extra_spaces(self, text):
                """
                Remove blank space but keep line breaks and add new lines for <li> tags.
                """
                soup = BeautifulSoup(str(text), "html.parser")
                for li in soup.find_all("li"):
                    li.insert_before("\n")
                    li.unwrap()
                text = soup.get_text()
                lines = text.splitlines()
                non_blank_lines = [line.strip() for line in lines if line.strip()]
                cleaned_text = "\n".join(non_blank_lines)
                return cleaned_text

        book_resource = HorillaListViewResorce()

        # Export the data using the resource
        dataset = book_resource.export(queryset)

        # Set the response headers
        # file_name = self.export_file_name
        if export_format == "json":
            json_data = json.loads(dataset.export("json"))
            response = HttpResponse(
                json.dumps(json_data, indent=4), content_type="application/json"
            )
            response["Content-Disposition"] = (
                f'attachment; filename="{self.export_file_name}.json"'
            )
            return response

        # CSV
        elif export_format == "csv":
            csv_data = dataset.export("csv")
            response = HttpResponse(csv_data, content_type="text/csv")
            response["Content-Disposition"] = (
                f'attachment; filename="{self.export_file_name}.csv"'
            )
            return response
        elif export_format == "pdf":

            headers = dataset.headers
            rows = dataset.dict

            # Render to HTML using a template
            html_string = render_to_string(
                "generic/export_pdf.html",
                {
                    "headers": headers,
                    "rows": rows,
                },
            )

            # Convert HTML to PDF using xhtml2pdf
            result = io.BytesIO()
            pisa_status = pisa.CreatePDF(html_string, dest=result)

            if pisa_status.err:
                return HttpResponse("PDF generation failed", status=500)

            # Return response
            response = HttpResponse(result.getvalue(), content_type="application/pdf")
            response["Content-Disposition"] = (
                f'attachment; filename="{self.export_file_name}.pdf"'
            )
            return response

        # response = HttpResponse(
        #     dataset.export("xlsx"), content_type="application/vnd.ms-excel"
        # )
        # response["Content-Disposition"] = (
        #     f'attachment; filename="{self.export_file_name}.xls"'
        # )
        json_data = json.loads(dataset.export("json"))
        headers = list(json_data[0].keys()) if json_data else []

        wb = Workbook()
        ws = wb.active
        ws.title = "Exported Data"

        # Styling
        header_fill = PatternFill(
            start_color="FFD700", end_color="FFD700", fill_type="solid"
        )
        bold_font = Font(bold=True)
        thin_border = Border(
            left=Side(style="thin"),
            right=Side(style="thin"),
            top=Side(style="thin"),
            bottom=Side(style="thin"),
        )
        wrap_alignment = Alignment(vertical="top", wrap_text=True)

        # Write headers
        for col_idx, header in enumerate(headers, start=1):
            cell = ws.cell(row=1, column=col_idx, value=header)
            cell.fill = header_fill
            cell.font = bold_font
            cell.border = thin_border
            cell.alignment = Alignment(
                horizontal="center", vertical="center", wrap_text=True
            )

        # Write data rows
        for row_idx, item in enumerate(json_data, start=2):
            for col_idx, key in enumerate(headers, start=1):
                value = item.get(key, "")
                # Convert lists to newline-separated string
                if isinstance(value, list):
                    value = "\n".join(str(v) for v in value)
                elif isinstance(value, dict):
                    value = json.dumps(
                        value, ensure_ascii=False
                    )  # or format it as needed
                cell = ws.cell(row=row_idx, column=col_idx, value=value)
                cell.border = thin_border
                cell.alignment = wrap_alignment

        # Auto-fit column widths
        for col_cells in ws.columns:
            max_len = max(len(str(cell.value or "")) for cell in col_cells)
            col_letter = get_column_letter(col_cells[0].column)
            ws.column_dimensions[col_letter].width = min(max_len + 5, 50)

        # Output to Excel
        output = io.BytesIO()
        wb.save(output)
        output.seek(0)

        response = HttpResponse(
            output.read(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        response["Content-Disposition"] = 'attachment; filename="exported_data.xlsx"'
        return response


@method_decorator(login_required, name="dispatch")
@method_decorator(manager_can_enter(perm="recruitment.view_candidate"), name="dispatch")
class CardCandidates(HorillaCardView):
    """
    For card view
    """

    model = Candidate
    filter_class = CandidateFilter

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.search_url = reverse("card-candidate")

    details = {
        "image_src": "get_avatar",
        "title": "{get_full_name}",
        "subtitle": "{email} <br> {get_job_position}",
    }

    actions = [
        {
            "action": _("Convert to Employee"),
            "accessibility": "recruitment.cbv.accessibility.convert_emp",
            "attrs": """
                onclick="event.stopPropagation()
                return confirm('Are you sure you want to convert this candidate into an employee?')"
                href='{get_convert_to_emp}'
                class="oh-dropdown__link"

            """,
        },
        {
            "action": _("Add to Talent Pool"),
            "accessibility": "recruitment.cbv.accessibility.add_skill_zone",
            "attrs": """
                data-toggle="oh-modal-toggle"
                data-target="#genericModal"
                hx-get="{get_add_to_skill}"
                hx-target="#genericModalBody"
                class="oh-dropdown__link"

            """,
        },
        {
            "action": _("View candidate self tracking"),
            "accessibility": "recruitment.cbv.accessibility.check_candidate_self_tracking",
            "attrs": """
                href="{get_self_tracking_url}"
                class="oh-dropdown__link"
            """,
        },
        {
            "action": _("Request Document"),
            "accessibility": "recruitment.cbv.accessibility.request_document",
            "attrs": """
                data-toggle="oh-modal-toggle"
                data-target="#genericModal"
                hx-get="{get_document_request_doc}"
                hx-target="#genericModalBody"
                class="oh-dropdown__link"
            """,
        },
        {
            "action": _("Add to Rejected"),
            "accessibility": "recruitment.cbv.accessibility.add_reject",
            "attrs": """
                hx-target="#genericModalBody"
                hx-swap="innerHTML"
                data-toggle="oh-modal-toggle"
                data-target="#genericModal"
                hx-get="{get_add_to_reject}"
                class="oh-dropdown__link"

            """,
        },
        # "Edit Rejected Candidate" deliberately removed: rejection is terminal
        # and sends the candidate an email, so the reason is not re-opened
        # afterwards. It stays visible in History.
        {
            "action": _("Edit Profile"),
            "attrs": """
                hx-get="{get_update_url}?container=true"
                hx-target="#candidateMainContainer"
                hx-swap="innerHTML"
                class="oh-dropdown__link"

            """,
        },
        {
            "action": "archive_status",
            "attrs": """
                class="oh-dropdown__link"
                hx-post="{get_archive_action_url}"
                hx-swap="none"
                hx-confirm="Do you want to change the archive status of this candidate?"
                onclick="event.stopPropagation();"
            """,
        },
        {
            "action": _("Delete"),
            "attrs": """
                class="oh-dropdown__link oh-dropdown__link--danger"
                hx-post="{get_delete_url}"
                hx-swap="none"
                hx-confirm="Are you sure you want to delete this candidate?"
                onclick="event.stopPropagation();"
            """,
        },
    ]
    card_status_indications = [
        (
            "canceled--dot",
            _("Canceled"),
            """
            onclick="
                $('#applyFilter').closest('form').find('[name=canceled]').val('true');
                $('#applyFilter').click();
            "
            """,
        ),
        (
            "nothired--dot",
            _("Not Hired"),
            """
            onclick="
                $('#applyFilter').closest('form').find('[name=hired]').val('false');
                $('#applyFilter').click();
            "
            """,
        ),
        (
            "hired--dot",
            _("Hired"),
            """
            onclick="$('#applyFilter').closest('form').find('[name=hired]').val('true');
                $('#applyFilter').click();
            "
            """,
        ),
    ]
    card_status_class = "hired-{hired} canceled-{canceled}"
    card_attrs = """
                hx-get="{get_profile_url}?instance_ids={ordered_ids}"
                hx-target="#listContainer"
                hx-swap="innerHTML"
                hx-push-url="{get_individual_url}"
                """


@method_decorator(login_required, name="dispatch")
@method_decorator(manager_can_enter(perm="recruitment.view_candidate"), name="dispatch")
class CandidateNav(HorillaNavView):
    """
    For nav bar
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.search_url = reverse("list-candidate")
        self.create_attrs = f"""
                            hx-get="{reverse_lazy('candidate-create')}?container=true"
                            hx-target="#candidateMainContainer"
                            hx-swap="innerHTML"
                            """
        self.actions = []
        if has_export_access(self.request, Candidate):
            self.actions.append(
                {
                    "action": _("Export"),
                    "attrs": f"""
                 data-toggle="oh-modal-toggle"
                 data-target="#genericModal"
                 hx-get="{reverse('export')}"
                 hx-target="#genericModalBody"
                 hx-vals='js:{{"has_selection": (JSON.parse(document.getElementById("selectedInstances")?.getAttribute("data-ids")||"[]").length>0)}}'
                 """,
                }
            )
        self.actions += [
            {
                "action": _("Bulk mail"),
                "attrs": f"""
                data-toggle="oh-modal-toggle"
                data-target="#genericModal"
                hx-get="{reverse('send-mail')}"
                hx-target="#genericModalBody"
                """,
            },
            {
                "action": _("Create document request"),
                "attrs": f"""
                data-toggle="oh-modal-toggle"
                data-target="#objectCreateModal"
                hx-get="{reverse('candidate-document-request')}"
                hx-target="#objectCreateModalTarget"
                """,
            },
            {
                "action": _("Archive"),
                "attrs": """
                id="archiveCandidates"

                """,
            },
            {
                "action": _("Un-archive"),
                "attrs": """
                id="unArchiveCandidates"

                """,
            },
            {
                "action": _("Delete"),
                "attrs": """
                data-action = "delete"
                id="deleteCandidates"
                 new_init
                """,
            },
        ]

        self.view_types = [
            {
                "type": "list",
                "icon": "list-outline",
                "url": reverse("list-candidate"),
                "attrs": f"""
                            title='{_("List")}'
                            """,
            },
            {
                "type": "card",
                "icon": "grid-outline",
                "url": reverse("card-candidate"),
                "attrs": f"""
                            title='{_("Card")}'
                            """,
            },
        ]
        self.filter_instance = CandidateFilter()

    nav_title = _("Candidates")
    filter_body_template = "cbv/candidates/filter.html"
    filter_form_context_name = "form"
    search_swap_target = "#listContainer"
    group_by_fields = [
        ("recruitment_id", _("Recruitment")),
        ("job_position_id", _("Job Position")),
        ("hired", _("Hired")),
        ("country", _("Country")),
        ("stage_id", _("Stage")),
        ("joining_date", _("Date joining")),
        ("probation_end", _("Probation End")),
        ("offer_letter_status", _("Offer Letter Status")),
        ("rejected_candidate__reject_reason_id", _("Reject reason")),
        ("skillzonecandidate_set", _("Talent pool")),
    ]


@method_decorator(login_required, name="dispatch")
@method_decorator(hx_request_required, name="dispatch")
@method_decorator(manager_can_enter(perm="recruitment.view_candidate"), name="dispatch")
class ExportView(TemplateView):
    """
    For candidate export
    """

    template_name = "cbv/candidates/export.html"

    def get_context_data(self, **kwargs: Any):
        """
        Adds export fields and filter object to the context.
        """
        context = super().get_context_data(**kwargs)
        candidates = Candidate.objects.filter(is_active=True)
        export_column = CandidateExportForm()
        export_filter = CandidateFilter(queryset=candidates)
        context["export_column"] = export_column
        context["export_filter"] = export_filter
        context["hide_export_filters"] = self.request.GET.get("has_selection") == "true"
        return context


@method_decorator(login_required, name="dispatch")
@method_decorator(manager_can_enter(perm="recruitment.view_candidate"), name="dispatch")
class AddToRejectedCandidatesView(View):
    """
    Reject a candidate: mandatory remark, automatic candidate email.

    The save goes through recruitment.services.candidate.reject_candidate,
    which owns everything this view previously left undone. Saving the form
    directly only wrote a RejectedCandidate row: the candidate was never
    actually marked ``canceled``, never moved to the cancelled stage, no audit
    event was written, and -- the PRD requirement -- no email reached the
    candidate. The service does all four in one transaction, so the Pool, the
    pipeline and History cannot disagree about who was rejected.

    form.save() is deliberately NOT called: the service creates the
    RejectedCandidate row itself, and saving here too would race it.
    """

    template_name = "onboarding/rejection/form.html"

    def _instance(self, candidate_id):
        if not candidate_id:
            return None
        return RejectedCandidate.objects.filter(candidate_id=candidate_id).first()

    def get(self, request, *args, **kwargs):
        """
        get method
        """
        candidate_id = request.GET.get("candidate_id")
        form = RejectedCandidateForm(
            initial={"candidate_id": candidate_id},
            instance=self._instance(candidate_id),
        )
        return render(request, self.template_name, {"form": form})

    def post(self, request, *args, **kwargs):
        """
        post method
        """
        from recruitment.services import candidate as candidate_service
        from recruitment.services.errors import RecruitmentError

        candidate_id = request.GET.get("candidate_id")
        form = RejectedCandidateForm(
            request.POST, instance=self._instance(candidate_id)
        )
        if form.is_valid():
            # The candidate is taken from the validated form rather than the
            # query string, so a mismatched id cannot reject someone else.
            candidate = form.cleaned_data["candidate_id"]
            reasons = [
                reason.pk for reason in form.cleaned_data.get("reject_reason_id") or []
            ]
            try:
                rejected = candidate_service.reject_candidate(
                    request.user,
                    candidate.pk,
                    reason=form.cleaned_data["description"],
                    reject_reason_ids=reasons,
                )
            except RecruitmentError as error:
                form.add_error(None, str(error))
                return render(request, self.template_name, {"form": form})
            messages.success(request, _("Candidate rejected."))
            if getattr(rejected, "rejection_email_sent", None) is False:
                # The rejection stands; the notification did not go out. Say so
                # rather than leaving the user to assume the candidate knows.
                messages.warning(
                    request,
                    _(
                        "The rejection email could not be sent. Check the Mail "
                        "Server configuration under Settings."
                    ),
                )
            return HorillaRedirect(request)
        return render(request, self.template_name, {"form": form})


@method_decorator(login_required, name="dispatch")
@method_decorator(
    all_manager_can_enter(perm="recruitment.view_candidate"), name="dispatch"
)
class CandidateDetail(HorillaDetailedView):
    """
    Candidate detail
    """

    title = "Candidate Details"

    model = Candidate

    header = {"title": "get_full_name", "subtitle": "get_email", "avatar": "get_avatar"}

    body = [
        (_("Gender"), "gender"),
        (_("Phone"), "mobile"),
        (_("Stage"), "stage_drop_down"),
        (_("Rating"), "rating_bar"),
        (_("Recruitment"), "recruitment_id"),
        (_("Job Position"), "job_position_id__job_position"),
        (_("Interview Table"), "candidate_interview_view", True),
    ]

    cols = {
        "candidate_interview_view": 12,
    }
    action_method = "detail_actions"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)


@method_decorator(login_required, name="dispatch")
@method_decorator(
    all_manager_can_enter(perm="recruitment.change_candidate"), name="dispatch"
)
class ToSkillZoneFormView(HorillaFormView):
    """
    Form View
    """

    model = SkillZoneCandidate
    form_class = ToSkillZoneForm
    new_display_title = _("Add to Talent Pool")

    def get_context_data(self, **kwargs):
        """
        Returns context with form and candidate data.
        """
        context = super().get_context_data(**kwargs)
        candidate_id = self.kwargs.get("cand_id")
        candidate = Candidate.objects.get(id=candidate_id)
        form = self.form_class(
            initial={
                "candidate_id": candidate,
                "skill_zone_ids": SkillZoneCandidate.objects.filter(
                    candidate_id=candidate
                ).values_list("skill_zone_id", flat=True),
            }
        )
        context["form"] = form
        return context

    def form_invalid(self, form: Any) -> HttpResponse:
        """
        Handles and renders form errors or defers to superclass.
        """
        form = self.form_class(self.request.POST)
        if not form.is_valid():
            errors = form.errors.as_data()
            return render(
                self.request, self.template_name, {"form": form, "errors": errors}
            )
        return super().form_invalid(form)

    def form_valid(self, form: ToSkillZoneForm) -> HttpResponse:
        """
        Handles valid form submission and saves rejected candidate reason.
        """
        if form.is_valid():
            candidate_id = self.kwargs.get("cand_id")
            candidate = Candidate.objects.get(id=candidate_id)
            self.form_class(
                initial={
                    "candidate_id": candidate,
                    "skill_zone_ids": SkillZoneCandidate.objects.filter(
                        candidate_id=candidate
                    ).values_list("skill_zone_id", flat=True),
                }
            )
            skill_zones = self.form.cleaned_data["skill_zone_ids"]
            for zone in skill_zones:
                if not SkillZoneCandidate.objects.filter(
                    candidate_id=candidate_id, skill_zone_id=zone
                ).exists():
                    zone_candidate = SkillZoneCandidate()
                    zone_candidate.candidate_id = candidate
                    zone_candidate.skill_zone_id = zone
                    zone_candidate.reason = self.form.cleaned_data["reason"]
                    zone_candidate.save()
            message = "Candidate added to talent pool successfully"
            messages.success(self.request, _(message))
            return self.HttpResponse()
        return super().form_valid(form)


@method_decorator(login_required, name="dispatch")
@method_decorator(
    all_manager_can_enter(perm="recruitment.change_candidate"), name="dispatch"
)
class RejectReasonFormView(HorillaFormView):
    """
    Reject a candidate: mandatory remark, automatic candidate email.

    This is the view the pipeline's Reject action actually reaches. The save
    goes through recruitment.services.candidate.reject_candidate, which owns
    everything this view previously left undone: it only wrote a
    RejectedCandidate row, so the candidate was never actually marked
    ``canceled``, never moved to the cancelled stage, no audit event was
    written, and -- the PRD requirement -- no email reached the candidate. The
    Pool read them as rejected (it reconciles the row) while the pipeline still
    showed them as active, and nothing was ever sent.

    form.save() is deliberately NOT called: the service creates the
    RejectedCandidate row itself, and saving here too would race it.
    """

    model = RejectedCandidate
    form_class = RejectedCandidateForm
    new_display_title = "Reject Candidate"
    # Hidden per PRD (remark only, no reason picker):
    # dynamic_create_fields = [("reject_reason_id", DynamicRejectReasonFormView)]
    template_name = "candidate/candidate_rejection_form.html"

    def get_initial(self) -> dict:
        initial = super().get_initial()
        initial["candidate_id"] = self.request.GET.get("candidate_id")
        return initial

    def init_form(self, *args, data={}, files={}, instance=None, **kwargs):
        candidate_id = self.request.GET.get("candidate_id")
        instance = RejectedCandidate.objects.filter(candidate_id=candidate_id).first()
        return super().init_form(
            *args, data=data, files=files, instance=instance, **kwargs
        )

    def form_valid(self, form: RejectedCandidateForm) -> HttpResponse:
        """
        Reject the candidate through the service, then report what happened.
        """
        from recruitment.services import candidate as candidate_service
        from recruitment.services.errors import RecruitmentError

        if not form.is_valid():
            return super().form_valid(form)

        # Taken from the validated form rather than the query string, so a
        # mismatched id cannot reject someone else.
        candidate = form.cleaned_data["candidate_id"]
        reasons = [
            reason.pk for reason in form.cleaned_data.get("reject_reason_id") or []
        ]
        try:
            rejected = candidate_service.reject_candidate(
                self.request.user,
                candidate.pk,
                reason=form.cleaned_data["description"],
                reject_reason_ids=reasons,
            )
        except RecruitmentError as error:
            form.add_error(None, str(error))
            return super().form_invalid(form)

        messages.success(self.request, _("Candidate rejected."))
        if getattr(rejected, "rejection_email_sent", None) is False:
            # The rejection stands; the notification did not go out. Say so
            # rather than leaving the user to assume the candidate knows.
            messages.warning(
                self.request,
                _(
                    "The rejection email could not be sent. Check the Mail "
                    "Server configuration under Settings."
                ),
            )
        return self.HttpResponse()
