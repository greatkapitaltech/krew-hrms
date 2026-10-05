"""
Worker class and grade masters (company-wise code / name lists used by payroll
eligibility conditions). Same list / nav / form pattern as employee type.
"""

from typing import Any

from django.contrib import messages
from django.http import HttpResponse
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.utils.translation import gettext_lazy as _

from base.filters import GradeFilter, WorkerClassFilter
from base.forms import GradeForm, WorkerClassForm
from base.models import Grade, WorkerClass
from horilla_views.cbv_methods import login_required, permission_required
from horilla_views.generic.cbv.views import (
    HorillaFormView,
    HorillaListView,
    HorillaNavView,
)


def _master_views(model, filter_class, form_class, title, list_url, create_url):
    """Build the list, nav and form views for one code / name master."""
    label = model._meta.model_name
    perm = f"base.%s_{label}"

    @method_decorator(login_required, name="dispatch")
    @method_decorator(permission_required(perm=perm % "view"), name="dispatch")
    class ListView(HorillaListView):
        show_toggle_form = False
        # Each Configuration tab has its own list container (see config_tab.html).
        selected_instances_key_id = f"selectedInstances_{label}"
        columns = [(_("Code"), "code"), (_("Name"), "name")]
        header_attrs = {"code": """ style="width:160px !important;" """}

        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.view_id = f"{label}_list"
            self.search_url = reverse(list_url)
            self.actions = []
            if self.request.user.has_perm(perm % "change"):
                self.actions.append(
                    {
                        "action": _("Edit"),
                        "icon": "create-outline",
                        "attrs": """
                        class="oh-btn oh-btn--light-bkg oh-btn--sq-sm"
                        hx-get="{get_update_url}"
                        hx-target="#genericModalBody"
                        data-toggle="oh-modal-toggle"
                        data-target="#genericModal"
                        """,
                    }
                )
            if self.request.user.has_perm(perm % "delete"):
                self.actions.append(
                    {
                        "action": _("Delete"),
                        "icon": "trash-outline",
                        "attrs": """
                        class="oh-btn oh-btn--danger oh-btn--sq-sm"
                        hx-get="{get_delete_url}"
                        data-toggle="oh-modal-toggle"
                        data-target="#deleteConfirmation"
                        hx-target="#deleteConfirmationBody"
                        """,
                    }
                )

    ListView.model = model
    ListView.filter_class = filter_class

    @method_decorator(login_required, name="dispatch")
    @method_decorator(permission_required(perm=perm % "view"), name="dispatch")
    class NavView(HorillaNavView):
        template_name = "generic/inline_nav.html"
        search_swap_target = f"#listContainer_{label}"

        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.search_url = reverse(list_url)
            self.filter_instance = filter_class()
            if self.request.user.has_perm(perm % "add"):
                self.create_attrs = f"""
                    onclick="event.stopPropagation();"
                    data-toggle="oh-modal-toggle"
                    data-target="#genericModal"
                    hx-target="#genericModalBody"
                    hx-get="{reverse(create_url)}"
                """

    NavView.nav_title = title

    @method_decorator(login_required, name="dispatch")
    @method_decorator(permission_required(perm=perm % "add"), name="dispatch")
    class FormView(HorillaFormView):
        new_display_title = _("Create %(title)s") % {"title": title}

        def form_valid(self, form) -> HttpResponse:
            if form.is_valid():
                updating = bool(form.instance.pk)
                form.save()
                messages.success(
                    self.request,
                    _("%(title)s updated.") % {"title": title}
                    if updating
                    else _("%(title)s created.") % {"title": title},
                )
                return self.HttpResponse()
            return super().form_valid(form)

    FormView.model = model
    FormView.form_class = form_class

    return ListView, NavView, FormView


WorkerClassListView, WorkerClassNavView, WorkerClassFormView = _master_views(
    WorkerClass,
    WorkerClassFilter,
    WorkerClassForm,
    _("Worker Classes"),
    "workerclass-list",
    "workerclass-create-view",
)
GradeListView, GradeNavView, GradeFormView = _master_views(
    Grade,
    GradeFilter,
    GradeForm,
    _("Grades"),
    "grade-list",
    "grade-create-view",
)
