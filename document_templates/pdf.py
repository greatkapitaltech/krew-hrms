"""
pdf.py

Renders an already-merged HTML string to PDF bytes via wkhtmltopdf (pdfkit).
Mirrors the pdf_options tuned in payroll/views/views.py's
generate_payslip_pdf, which takes a template path + context and calls
render_to_string internally - we already have the final HTML by the time we
get here (merge fields resolved), so this is the standalone "HTML -> PDF"
half of that same approach rather than a reusable import.
"""

import pdfkit

from horilla.horilla_middlewares import _thread_locals

PAGE_WRAPPER = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<style>
  body {{ font-family: 'Inter', Arial, sans-serif; margin: 0; padding: 0; }}
  .oh-document-body {{ padding: 24px; }}
</style>
</head>
<body>
<div class="oh-document-body">{content}</div>
</body>
</html>"""


def html_to_pdf(content_html: str) -> bytes:
    html_content = PAGE_WRAPPER.format(content=content_html)

    request = getattr(_thread_locals, "request", None)
    cookies = request.META.get("HTTP_COOKIE", "") if request else ""

    pdf_options = {
        "page-size": "A4",
        "margin-top": "10mm",
        "margin-bottom": "10mm",
        "margin-left": "10mm",
        "margin-right": "10mm",
        "encoding": "UTF-8",
        "enable-local-file-access": None,  # needed to load local TemplateAsset images
        "dpi": 300,
    }
    if cookies:
        pdf_options.update(
            {
                "custom-header": [("Cookie", cookies)],
                "custom-header-propagation": None,
            }
        )

    try:
        return pdfkit.from_string(html_content, False, options=pdf_options)
    except OSError as exc:
        # wkhtmltopdf not installed / not on PATH in this environment - fail
        # loudly rather than silently producing a blank/garbage PDF.
        raise RuntimeError("wkhtmltopdf is not available; cannot render document to PDF.") from exc
