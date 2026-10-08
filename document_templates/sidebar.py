"""
document_templates/sidebar.py
"""

from django.urls import reverse_lazy
from django.utils.translation import gettext_lazy as _

MENU = _("Document Templates")
IMG_SRC = "images/ui/unknown_document.svg"
ACCESSIBILITY = "document_templates.sidebar.menu_accessibility"

SUBMENUS = [
    {
        "menu": _("Templates"),
        "redirect": reverse_lazy("document-template-list"),
        "accessibility": "document_templates.sidebar.templates_accessibility",
    },
    {
        "menu": _("Assets"),
        "redirect": reverse_lazy("document-template-asset-list"),
        "accessibility": "document_templates.sidebar.assets_accessibility",
    },
    {
        "menu": _("Generated Documents"),
        "redirect": reverse_lazy("generated-document-list"),
        "accessibility": "document_templates.sidebar.generated_accessibility",
    },
]


def menu_accessibility(request, menu, user_perms, *args, **kwargs):
    return (
        request.user.has_perm("document_templates.view_documenttemplate")
        or request.user.has_perm("document_templates.view_templateasset")
        or request.user.has_perm("document_templates.view_generateddocument")
    )


def templates_accessibility(request, submenu, user_perms, *args, **kwargs):
    return request.user.has_perm("document_templates.view_documenttemplate")


def assets_accessibility(request, submenu, user_perms, *args, **kwargs):
    return request.user.has_perm("document_templates.view_templateasset")


def generated_accessibility(request, submenu, user_perms, *args, **kwargs):
    return request.user.has_perm("document_templates.view_generateddocument")
