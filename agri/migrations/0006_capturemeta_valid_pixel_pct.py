# Generated for Stage 10 Phase 1 (cloud/quality awareness) -- see
# precise-agric/stages/stage-10-sentinel-roadmap.md.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('agri', '0005_capture_source_and_computed_by'),
    ]

    operations = [
        migrations.AddField(
            model_name='capturemeta',
            name='valid_pixel_pct',
            field=models.FloatField(
                blank=True, null=True,
                help_text="Satellite only: % of pixels not cloud/shadow/no-data, from Sentinel's SCL band",
                verbose_name='Valid pixel %'),
        ),
    ]
