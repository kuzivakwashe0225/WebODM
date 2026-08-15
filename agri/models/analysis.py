from django.db import models
from django.conf import settings
from django.contrib.postgres import fields
from django.utils.translation import gettext_lazy as _


class AnalysisRun(models.Model):
    """
    One analysis run over an APPROVED boundary of a capture (Task). Fans out to
    the individual analyses (AnalysisResult rows), then awaits agronomist review.
    Status lives here, never on Task.status.
    """
    PENDING = 'PENDING'
    RUNNING = 'RUNNING'
    PENDING_REVIEW = 'PENDING_REVIEW'
    APPROVED = 'APPROVED'
    REJECTED = 'REJECTED'
    FAILED = 'FAILED'
    STATUS_CHOICES = (
        (PENDING, _('Pending')),
        (RUNNING, _('Running')),
        (PENDING_REVIEW, _('Pending review')),
        (APPROVED, _('Approved')),
        (REJECTED, _('Rejected')),
        (FAILED, _('Failed')),
    )

    task = models.ForeignKey('app.Task', on_delete=models.CASCADE, related_name='analysis_runs',
                             verbose_name=_("Capture"))
    boundary = models.ForeignKey('agri.Boundary', on_delete=models.CASCADE,
                                 related_name='analysis_runs')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=PENDING,
                              verbose_name=_("Status"))
    index_used = models.CharField(max_length=32, blank=True, default='',
                                  help_text=_("Vegetation index used (e.g. NDVI, ExG)"))
    triggered_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                     null=True, blank=True, related_name='triggered_analyses')
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                    null=True, blank=True, related_name='reviewed_analyses')
    error = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = _("Analysis Run")
        verbose_name_plural = _("Analysis Runs")
        ordering = ['-created_at']

    def __str__(self):
        return "AnalysisRun #%s [%s]" % (self.pk, self.status)


class AnalysisResult(models.Model):
    """One output of an AnalysisRun (one of the analysis services)."""
    RGB_INDEX = 'rgb_index'
    PLANT_HEALTH = 'plant_health'
    GRID = 'grid'
    WEED = 'weed'
    CANOPY = 'canopy'
    REPORT = 'report'
    KIND_CHOICES = (
        (RGB_INDEX, _('RGB Index')),
        (PLANT_HEALTH, _('Plant Health')),
        (GRID, _('Grid Analysis')),
        (WEED, _('Weed Mapping')),
        (CANOPY, _('Canopy Cover')),
        (REPORT, _('Report')),
    )

    run = models.ForeignKey(AnalysisRun, on_delete=models.CASCADE, related_name='results')
    kind = models.CharField(max_length=20, choices=KIND_CHOICES, verbose_name=_("Kind"))
    asset_path = models.CharField(max_length=1024, blank=True, default='',
                                  help_text=_("Path (relative to the task assets dir) of the result asset"))
    stats = fields.JSONField(default=dict, blank=True, verbose_name=_("Statistics"))
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("Analysis Result")
        verbose_name_plural = _("Analysis Results")
        ordering = ['kind']

    def __str__(self):
        return "%s result for run #%s" % (self.kind, self.run_id)
