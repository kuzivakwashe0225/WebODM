from django.contrib.gis.db import models as gismodels
from django.db import models
from django.conf import settings
from django.utils.translation import gettext_lazy as _


class Boundary(models.Model):
    """
    A field boundary drawn on a Capture (an imported-orthophoto WebODM Task).

    Boundaries start as DRAFT and must be APPROVED (by an Admin/Agronomist)
    before analysis can run against them (see Stage 2+). Approval state lives
    here on the agri model, never on Task.status.
    """
    DRAFT = 'DRAFT'
    APPROVED = 'APPROVED'
    REJECTED = 'REJECTED'
    STATUS_CHOICES = (
        (DRAFT, _('Draft')),
        (APPROVED, _('Approved')),
        (REJECTED, _('Rejected')),
    )

    # Capture == an imported-orthophoto Task (we reuse app.Task, no separate model).
    # String ref avoids any import-order coupling with the app models.
    task = models.ForeignKey('app.Task', on_delete=models.CASCADE, related_name='boundaries',
                             verbose_name=_("Capture"),
                             help_text=_("Capture (imported orthophoto task) this boundary belongs to"))
    # Set when this boundary's geometry came from a synced AgriTrack Field
    # rather than being hand-drawn in WebODM (MVP: whichever is available is
    # used; a proper per-farm user choice between the two is a later increment
    # -- see precise-agric/architecture-and-plan.md AgriTrack integration notes).
    agri_field = models.ForeignKey('agri.AgriField', on_delete=models.SET_NULL,
                                   null=True, blank=True, related_name='boundaries',
                                   verbose_name=_("AgriTrack Field"))
    # Persistent field identity across captures (seasonal tracking). A boundary
    # is one capture's outline of this Field; runs are grouped by Field over time.
    # Nullable: boundaries without a Field are simply excluded from seasonal trends.
    field = models.ForeignKey('agri.Field', on_delete=models.SET_NULL,
                              null=True, blank=True, related_name='boundaries',
                              verbose_name=_("Field"))
    name = models.CharField(max_length=255, default='Field boundary', verbose_name=_("Name"))
    geom = gismodels.PolygonField(srid=4326, verbose_name=_("Geometry"),
                                  help_text=_("Field boundary polygon (WGS84)"))
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=DRAFT,
                              verbose_name=_("Status"))
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                   null=True, blank=True, related_name='created_boundaries')
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                    null=True, blank=True, related_name='approved_boundaries')
    approved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("Boundary")
        verbose_name_plural = _("Boundaries")
        ordering = ['-created_at']

    def __str__(self):
        return "%s [%s]" % (self.name, self.status)
