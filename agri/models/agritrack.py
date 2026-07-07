from django.contrib.gis.db import models as gismodels
from django.db import models
from django.utils.translation import gettext_lazy as _


class AgriFarm(models.Model):
    """
    Mirrors a Farm from the AgriTrack mobile app (external source of truth).

    farmer_id/agritrack_farm_id are AgriTrack's own IDs -- preserved exactly,
    never replaced with local ones (contract §3.1). One-to-one with our own
    Project (= Farm in this system, per the locked domain-model decision):
    created automatically the first time this farm is synced.
    """
    agritrack_farm_id = models.IntegerField(unique=True, db_index=True,
                                            verbose_name=_("AgriTrack Farm ID"))
    agritrack_farmer_id = models.IntegerField(null=True, blank=True,
                                              verbose_name=_("AgriTrack Farmer (User) ID"))
    project = models.OneToOneField('app.Project', on_delete=models.CASCADE,
                                   related_name='agri_farm', verbose_name=_("Farm (Project)"))
    name = models.CharField(max_length=255, blank=True, default='')
    location = models.CharField(max_length=255, blank=True, default='')
    # The authoritative farm boundary as last synced from AgriTrack. Advisory/
    # contextual at the farm level in this system -- analysis runs against
    # Field boundaries, not the farm outline.
    boundary = gismodels.PolygonField(srid=4326, null=True, blank=True)
    synced_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("AgriTrack Farm")
        verbose_name_plural = _("AgriTrack Farms")

    def __str__(self):
        return self.name or ("AgriTrack Farm #%s" % self.agritrack_farm_id)


class AgriField(models.Model):
    """
    Mirrors a Field from AgriTrack -- a persistent agronomic entity that can be
    scanned/captured many times over. Its boundary is authoritative per the
    contract (§3.2.1): WebODM must not silently overwrite it from imagery.

    A Boundary (agri.Boundary) may optionally link back here (boundary.agri_field)
    when its geometry was taken from this synced Field rather than hand-drawn --
    see agri/agritrack/sync.py for how that Boundary gets created.
    """
    agritrack_field_id = models.IntegerField(unique=True, db_index=True,
                                             verbose_name=_("AgriTrack Field ID"))
    farm = models.ForeignKey(AgriFarm, on_delete=models.CASCADE, related_name='fields')
    name = models.CharField(max_length=255, blank=True, default='')
    crop = models.CharField(max_length=255, blank=True, default='')
    area_ha = models.FloatField(null=True, blank=True)
    boundary = gismodels.PolygonField(srid=4326, verbose_name=_("Authoritative Boundary"))

    # Scan configuration (contract §4.9) -- informational for now; nothing in
    # this system currently schedules captures automatically from it.
    scan_frequency = models.CharField(max_length=16, blank=True, default='',
                                      help_text=_("daily | weekly | manual"))
    scan_preferred_time = models.CharField(max_length=5, blank=True, default='')
    scan_enabled = models.BooleanField(default=False)

    synced_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("AgriTrack Field")
        verbose_name_plural = _("AgriTrack Fields")

    def __str__(self):
        return self.name or ("AgriTrack Field #%s" % self.agritrack_field_id)
