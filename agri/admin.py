from django.contrib import admin
from agri.models import Boundary, AnalysisRun, AnalysisResult


@admin.register(Boundary)
class BoundaryAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'task', 'status', 'created_by', 'approved_by', 'created_at')
    list_filter = ('status',)
    search_fields = ('name',)
    readonly_fields = ('created_at', 'approved_at')
    # geom is edited via the map UI / API, not the admin form (avoids the GIS map widget)
    exclude = ('geom',)


class AnalysisResultInline(admin.TabularInline):
    model = AnalysisResult
    extra = 0
    readonly_fields = ('created_at',)


@admin.register(AnalysisRun)
class AnalysisRunAdmin(admin.ModelAdmin):
    list_display = ('id', 'task', 'boundary', 'status', 'index_used', 'triggered_by', 'created_at')
    list_filter = ('status',)
    readonly_fields = ('created_at', 'completed_at')
    inlines = [AnalysisResultInline]


@admin.register(AnalysisResult)
class AnalysisResultAdmin(admin.ModelAdmin):
    list_display = ('id', 'run', 'kind', 'asset_path', 'created_at')
    list_filter = ('kind',)
