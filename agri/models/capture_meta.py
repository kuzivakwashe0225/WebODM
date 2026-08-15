from django.db import models
from django.utils.translation import gettext_lazy as _


class CaptureMeta(models.Model):
    """
    Precise-Agric metadata for a Capture (an app.Task), kept in the agri app so
    the core Task model is untouched.

    Holds the *acquisition date* of the orthophoto -- the x-axis of the
    seasonal-progress graphs. Imported GeoTIFFs carry no reliable embedded date,
    so the user supplies it at upload time (default today); the raw-image and
    AgriTrack paths fall back to the task's created_at date.

    Also holds the imagery *source* (drone vs satellite). This matters because a
    satellite pixel covers ~100 m^2 vs a drone's few cm^2 -- the two are never
    directly comparable, so the seasonal API and the analysis fan-out
    (agri/services.py) both key off this to keep drone and satellite results
    visually and statistically separate (see precise-agric/stages/
    stage-9-satellite-monitoring.md §5).
    """
    DRONE = 'DRONE'
    SATELLITE = 'SATELLITE'
    SOURCE_CHOICES = (
        (DRONE, _('Drone')),
        (SATELLITE, _('Satellite')),
    )

    task = models.OneToOneField('app.Task', on_delete=models.CASCADE,
                                related_name='capture_meta', verbose_name=_("Capture"))
    capture_date = models.DateField(verbose_name=_("Capture date"),
                                    help_text=_("Date the imagery was acquired (flight date)"))
    source = models.CharField(max_length=16, choices=SOURCE_CHOICES, default=DRONE,
                              verbose_name=_("Source"),
                              help_text=_("Where the imagery came from (drone flight or satellite pass)"))
    # Satellite-only quality signal: % of pixels classified as usable (not
    # cloud/shadow/cirrus/no-data) via Sentinel's SCL band. Null for drone
    # captures (meaningless there) and for satellite pulls where the quality
    # check itself failed -- a missing value never blocks the import that
    # already succeeded ("flag, don't block", stage-10-sentinel-roadmap.md
    # Phase 1). Set by agri/remote_sense/sentinel_pull.py after import.
    valid_pixel_pct = models.FloatField(null=True, blank=True,
                                        verbose_name=_("Valid pixel %"),
                                        help_text=_("Satellite only: % of pixels not cloud/shadow/"
                                                    "no-data, from Sentinel's SCL band"))
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("Capture Metadata")
        verbose_name_plural = _("Capture Metadata")

    def __str__(self):
        return "Capture %s @ %s (%s)" % (self.task_id, self.capture_date, self.source)
