from django.contrib import admin

from .models import DocumentTemplate, GeneratedDocument, TemplateAsset

admin.site.register(DocumentTemplate)
admin.site.register(TemplateAsset)
admin.site.register(GeneratedDocument)
