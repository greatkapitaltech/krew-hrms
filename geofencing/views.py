from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.http import QueryDict
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.decorators import method_decorator
from django.utils.translation import gettext_lazy as _
from rest_framework import status
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from base.config_tiers import TIER_COMPANY
from base.models import Company
from geofencing.forms import GeoFencingSetupForm
from geofencing.methods import check_geo_fence

from .models import GeoFencing
from .serializers import *


class GeoFencingSetupGetPostAPIView(APIView):
    """
    Reads/creates this company's Company Default boundary (the
    tier=COMPANY row) specifically -- Department overrides and
    Employee-Type exemptions are separate rows, managed through
    GeoFencingSetupPutDeleteAPIView by pk like any other row, not through
    this single-object endpoint.
    """

    permission_classes = [IsAuthenticated]

    @method_decorator(
        permission_required("geofencing.view_geofencing", raise_exception=True),
        name="dispatch",
    )
    def get(self, request):
        company = request.user.employee_get.get_company()
        location = get_object_or_404(GeoFencing, company=company, tier=TIER_COMPANY)
        serializer = GeoFencingSetupSerializer(location)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @method_decorator(
        permission_required("geofencing.add_geofencing", raise_exception=True),
        name="dispatch",
    )
    def post(self, request):
        data = request.data
        if isinstance(data, QueryDict):
            data = data.dict()
        data["tier"] = TIER_COMPANY
        if not request.user.is_superuser:
            company = request.user.employee_get.get_company()
            if company:
                data["company"] = company.id
        serializer = GeoFencingSetupSerializer(data=data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class GeoFencingSetupPutDeleteAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def get_location(self, pk):
        try:
            return GeoFencing.objects.get(pk=pk)
        except Exception as e:
            raise serializers.ValidationError(e)

    @method_decorator(
        permission_required("geofencing.change_geofencing", raise_exception=True),
        name="dispatch",
    )
    def put(self, request, pk):
        location = self.get_location(pk)
        company = request.user.employee_get.get_company()
        if request.user.is_superuser or company == location.company:
            serializer = GeoFencingSetupSerializer(
                location, data=request.data, partial=True
            )
            if serializer.is_valid():
                serializer.save()
                return Response(serializer.data, status=status.HTTP_200_OK)
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        raise serializers.ValidationError(_("Access Denied.."))

    @method_decorator(
        permission_required("geofencing.delete_geofencing", raise_exception=True),
        name="dispatch",
    )
    def delete(self, request, pk):
        location = self.get_location(pk)
        company = request.user.employee_get.get_company()
        if request.user.is_superuser or company == location.company:
            location.delete()
            return Response(
                {"message": "GeoFencing location deleted successfully"},
                status=status.HTTP_200_OK,
            )
        raise serializers.ValidationError(_("Access Denied.."))


class GeoFencingEmployeeLocationCheckAPIView(APIView):
    """
    Standalone location check (e.g. for a mobile "you're in range"
    indicator, independent of an actual punch). Delegates to the exact
    same check_geo_fence() used by ClockInAPIView/ClockOutAPIView, so
    there is one single source of truth for the distance/violation
    logic -- this endpoint never re-implements it.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = EmployeeLocationSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        employee = request.user.employee_get
        latitude = request.data.get("latitude")
        longitude = request.data.get("longitude")
        result = check_geo_fence(employee, latitude, longitude)

        if result.allowed and not result.violation and not result.unverified:
            return Response({"message": "Inside the geofence"}, status=status.HTTP_200_OK)
        if result.unverified:
            return Response(
                {"message": "Could not verify your location"},
                status=status.HTTP_200_OK,
            )
        return Response(
            {"message": "Outside the geofence"}, status=status.HTTP_400_BAD_REQUEST
        )


class GeoFencingSetUpPermissionCheck(APIView):
    permission_classes = [IsAuthenticated]

    @method_decorator(
        permission_required("geofencing.view_geofencing", raise_exception=True),
        name="dispatch",
    )
    def get(self, request):
        return Response(status=200)


def get_company(request):
    try:
        selected_company = request.session.get("selected_company")
        if selected_company == "all":
            return None
        company = Company.objects.get(id=selected_company)
        return company
    except Exception as e:
        raise serializers.ValidationError(e)


def get_company_location(request):
    company = get_company(request)
    try:
        location = GeoFencing.objects.get(company=company, tier=TIER_COMPANY)
        return location
    except Exception as e:
        raise serializers.ValidationError(e)


@login_required
@permission_required("geofencing.add_localbackup")
def geo_location_config(request):
    try:
        form = GeoFencingSetupForm(instance=get_company_location(request))
    except:
        form = GeoFencingSetupForm()
    if request.method == "POST":
        try:
            form = GeoFencingSetupForm(
                request.POST, instance=get_company_location(request)
            )
        except:
            form = GeoFencingSetupForm(request.POST)
        if form.is_valid():
            geofencing = form.save(commit=False)
            geofencing.company = get_company(request)
            geofencing.tier = TIER_COMPANY
            geofencing.save()
            messages.success(request, _("Geofencing config created successfully."))
        else:
            messages.info(request, _("Not valid"))
    return render(request, "geo_config.html", {"form": form})
