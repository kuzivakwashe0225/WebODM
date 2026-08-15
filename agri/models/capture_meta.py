from django.db import models
from django.utils.translation import gettext_lazy as _


class CaptureMeta(models.Model):
    """
    Precise-Agric metadata for a Capture (an app.Task), kept in the agri app so
    the core Task model is untouched.

    Currently holds the *acquisition date* of the orthophoto -- the x-axis of the
    seasonal-progress graphs. Imported GeoTIFFs carry no reliable embedded date,
    so the user supplies it at upload time (default today); the raw-image and
    AgriTrack paths fall back to the task's created_at date.
    """
    task = models.OneToOneField('app.Task', on_delete=models.CASCADE,
                                related_name='capture_meta', verbose_name=_("Capture"))
    capture_date = models.DateField(verbose_name=_("Capture date"),
                                    help_text=_("Date the imagery was acquired (flight date)"))
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("Capture Metadata")
        verbose_name_plural = _("Capture Metadata")

    def __str__(self):
        return "Capture %s @ %s" % (self.task_id, self.capture_date)
