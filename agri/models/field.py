from django.db import models
from django.utils.translation import gettext_lazy as _


class Field(models.Model):
    """
    A persistent agronomic field within a Farm (Project), giving a field a
    *stable identity across captures* so its analysis results can be tracked
    over a whole season.

    A Boundary (drawn or synced) attaches to one Field via Boundary.field; each
    capture of the same physical field reuses the same Field, and the seasonal
    view groups AnalysisRuns by Field over time. For AgriTrack-synced fields,
    `agri_field` links this to the external AgriField so those get trends too.
    """
    project = models.ForeignKey('app.Project', on_delete=models.CASCADE,
                                related_name='agri_fields', verbose_name=_("Farm (Project)"))
    name = models.CharField(max_length=255, verbose_name=_("Name"))
    # Set when this Field corresponds to an AgriTrack-synced field (auto-created
    # in agri/agritrack/sync.py). Nullable: hand-drawn fields have no AgriField.
    agri_field = models.OneToOneField('agri.AgriField', on_delete=models.SET_NULL,
                                      null=True, blank=True, related_name='field',
                                      verbose_name=_("AgriTrack Field"))
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("Field")
        verbose_name_plural = _("Fields")
        ordering = ['name']
        unique_together = ('project', 'name')

    def __str__(self):
        return self.name
