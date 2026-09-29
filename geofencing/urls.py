from django.urls import path

from .cbv.geo_fencing_rule import (
    GeoFencingRuleFormView,
    GeoFencingRuleListView,
    GeoFencingRuleNav,
    GeoFencingRulePageView,
)
from .views import *

urlpatterns = [
    path("setup/", GeoFencingSetupGetPostAPIView.as_view()),
    path("setup/<int:pk>/", GeoFencingSetupPutDeleteAPIView.as_view()),
    path("setup-check/", GeoFencingSetUpPermissionCheck.as_view()),
    path("location-check/", GeoFencingEmployeeLocationCheckAPIView.as_view()),
    path("config/", geo_location_config, name="geo-config"),
    path("rules/", GeoFencingRulePageView.as_view(), name="geo-fencing-rule-view"),
    path(
        "rules/list/", GeoFencingRuleListView.as_view(), name="geo-fencing-rule-list"
    ),
    path("rules/nav/", GeoFencingRuleNav.as_view(), name="geo-fencing-rule-nav"),
    path(
        "rules/create/",
        GeoFencingRuleFormView.as_view(),
        name="geo-fencing-rule-create",
    ),
    path(
        "rules/<int:pk>/update/",
        GeoFencingRuleFormView.as_view(),
        name="geo-fencing-rule-update",
    ),
]
