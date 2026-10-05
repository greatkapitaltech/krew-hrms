"""
Earnings and deductions APIs: create, update, publish, deactivate.

Each call is synchronous: one database transaction that either saves everything
and returns the saved state, or rolls back and returns an error. The same four
routes exist under /earnings/ and /deductions/; the route fixes the component
type, so an earning id on /deductions/ returns 404.

Company-wise: company_id is never sent. It is the caller's selected company
(session), or for token clients the ``X-Company-Id`` header limited to the
companies the user may access, else the user's own company.
"""

from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from horilla.horilla_middlewares import get_selected_company, set_selected_company
from krew_payroll.methods.pay_components import services
from krew_payroll.methods.pay_components.serializers import component_json, iso
from krew_payroll.methods.pay_components.services import PayComponentError
from krew_payroll.models.pay_components import ComponentType

ROUTE_TYPES = {"earnings": ComponentType.EARNING, "deductions": ComponentType.DEDUCTION}


def _resolve_company(request):
    """Return the company id this request acts in, or None."""
    from base.auth_backends import get_allowed_company_ids, get_write_company_id
    from base.models import Company

    selected = get_selected_company()
    if selected not in (None, "", "all"):
        return int(selected)
    header = request.headers.get("X-Company-Id")
    user = request.user
    if header:
        try:
            company_id = int(header)
        except ValueError:
            return None
        if user.is_superuser:
            return company_id if Company.objects.filter(pk=company_id).exists() else None
        return company_id if company_id in get_allowed_company_ids(user) else None
    return get_write_company_id(user)


class PayComponentAPIView(APIView):
    """Shared plumbing: company context, permission check, error mapping."""

    permission_classes = [IsAuthenticated]
    required_perm = "krew_payroll.change_paycomponent"

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        company_id = _resolve_company(request)
        self.company_id = company_id
        if company_id:
            set_selected_company(company_id)

    def guard(self, request, route):
        if route not in ROUTE_TYPES:
            return Response({"detail": "Unknown route"}, status=404)
        if not self.company_id:
            return Response(
                {"detail": "No company selected. Send X-Company-Id for a company you can access."},
                status=400,
            )
        if not request.user.has_perm(self.required_perm):
            return Response(
                {
                    "detail": "Only the company's Admin, VWS HR or Client HR can configure "
                    "earnings and deductions."
                },
                status=403,
            )
        return None

    def run(self, request, route, action):
        refused = self.guard(request, route)
        if refused:
            return refused
        try:
            return action(ROUTE_TYPES[route])
        except PayComponentError as exc:
            return Response(exc.payload, status=exc.status)


class PayComponentCreateAPIView(PayComponentAPIView):
    """POST /payroll/{earnings|deductions}/ — create a component and its DRAFT v1."""

    required_perm = "krew_payroll.add_paycomponent"

    def post(self, request, route):
        def action(type_):
            component, details = services.create_component(type_, request.data)
            return Response(component_json(component, details), status=201)

        return self.run(request, route, action)


class PayComponentVersionAPIView(PayComponentAPIView):
    """PATCH /payroll/{route}/{id}/versions/{v}/ — update a version (and the name)."""

    def patch(self, request, route, pk, version_no):
        def action(type_):
            component = services.get_component(type_, int(pk))
            details = services.get_version(component, int(version_no))
            details, checks = services.update_version(details, request.data)
            data = component_json(details.component, details)
            data["checks"] = {
                "ready_to_publish": not checks["total"],
                "errors": checks["errors"],
                "warnings": checks["warnings"],
            }
            return Response(data, status=200)

        return self.run(request, route, action)


class PayComponentPublishAPIView(PayComponentAPIView):
    """POST /payroll/{route}/{id}/versions/{v}/publish/ — validate, publish, close the previous version."""

    def post(self, request, route, pk, version_no):
        def action(type_):
            component = services.get_component(type_, int(pk))
            details = services.get_version(component, int(version_no))
            details, closed, warnings = services.publish_version(details)
            data = component_json(component, details)
            data["closed_versions"] = [
                {"version_no": v.version_no, "effective_to": iso(v.effective_to)} for v in closed
            ]
            data["warnings"] = warnings
            return Response(data, status=200)

        return self.run(request, route, action)


class PayComponentDeactivateAPIView(PayComponentAPIView):
    """POST /payroll/{route}/{id}/deactivate/ — mark inactive from the next pay cycle."""

    def post(self, request, route, pk):
        def action(type_):
            component = services.get_component(type_, int(pk))
            dependants = services.deactivate_component(component)
            return Response(services.deactivate_json(component, dependants), status=200)

        return self.run(request, route, action)
