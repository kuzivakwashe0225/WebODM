from rest_framework import serializers
from app.api.fields import PolygonGeometryField
from agri.geo import geom_to_geojson
from agri.models import Boundary, AnalysisRun, AnalysisResult, Field


class FieldSerializer(serializers.ModelSerializer):
    class Meta:
        model = Field
        fields = ('id', 'project', 'name', 'agri_field', 'created_at')
        read_only_fields = ('agri_field', 'created_at')


class BoundarySerializer(serializers.ModelSerializer):
    # GeoJSON in/out, reusing WebODM's existing field. Not required: when
    # agri_field is given instead, the view derives geom from that Field's
    # synced (authoritative) boundary -- see BoundaryViewSet.perform_create.
    geom = PolygonGeometryField(required=False)
    # Write-only convenience: assign a persistent Field by name (created if new
    # within the farm) so a drawn boundary can be tracked across the season
    # without a separate round-trip. `field` (id) is also accepted directly.
    field_name = serializers.CharField(write_only=True, required=False, allow_blank=True)

    class Meta:
        model = Boundary
        fields = ('id', 'task', 'name', 'geom', 'agri_field', 'field', 'field_name',
                  'status', 'created_by', 'approved_by', 'approved_at', 'created_at')
        read_only_fields = ('status', 'created_by', 'approved_by', 'approved_at', 'created_at')

    def to_representation(self, instance):
        data = super().to_representation(instance)
        # PolygonGeometryField reads via geom.geojson, which swaps to [lat, lng]
        # under GDAL 3 (see agri/geo.py). Emit correct [lng, lat] so Leaflet places
        # boundaries on the orthophoto instead of on the far side of the planet.
        if instance.geom is not None:
            data['geom'] = geom_to_geojson(instance.geom)
        return data


class AnalysisResultSerializer(serializers.ModelSerializer):
    class Meta:
        model = AnalysisResult
        fields = ('id', 'kind', 'asset_path', 'stats', 'created_at')


class AnalysisRunSerializer(serializers.ModelSerializer):
    results = AnalysisResultSerializer(many=True, read_only=True)
    # Leaflet-ready heatmap tile URL (reuses WebODM's tiler; see agri/tiles.py).
    # None until the run has a plant_health result.
    plant_health_tile_url = serializers.SerializerMethodField()

    class Meta:
        model = AnalysisRun
        fields = ('id', 'task', 'boundary', 'status', 'computed_by', 'index_used', 'triggered_by',
                  'reviewed_by', 'error', 'created_at', 'completed_at', 'results',
                  'plant_health_tile_url')
        read_only_fields = ('task', 'status', 'computed_by', 'index_used', 'triggered_by',
                            'reviewed_by', 'error', 'created_at', 'completed_at', 'results')

    def get_plant_health_tile_url(self, run):
        from agri.tiles import plant_health_tile_url
        return plant_health_tile_url(run)
