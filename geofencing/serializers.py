from rest_framework import serializers

from .models import GeoFencing


class GeoFencingSetupSerializer(serializers.ModelSerializer):
    class Meta:
        model = GeoFencing
        fields = "__all__"

    # No geocoding validation here -- GeoFencing.save() already runs
    # full_clean() (which includes it for boundary-bearing tiers) before
    # every save, so duplicating the Nominatim call here would just mean
    # doing the same external lookup twice per request.


class EmployeeLocationSerializer(serializers.Serializer):
    latitude = serializers.FloatField()
    longitude = serializers.FloatField()
