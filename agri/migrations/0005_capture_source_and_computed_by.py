# Generated for Stage 9 (satellite monitoring) -- see
# precise-agric/stages/stage-9-satellite-monitoring.md §5.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('agri', '0004_auto_20260706_2049'),
    ]

    operations = [
        migrations.AddField(
            model_name='capturemeta',
            name='source',
            field=models.CharField(choices=[('DRONE', 'Drone'), ('SATELLITE', 'Satellite')],
                                   default='DRONE', max_length=16,
                                   help_text='Where the imagery came from (drone flight or satellite pass)',
                                   verbose_name='Source'),
        ),
        migrations.AddField(
            model_name='analysisrun',
            name='computed_by',
            field=models.CharField(
                choices=[('WEBODM', 'Precise-Agric (WebODM)'), ('SENTINEL', 'Sentinel remote-sense engine')],
                default='WEBODM', max_length=16,
                help_text="Which engine produced these results -- our own analysis fan-out, or numbers "
                          "submitted by Sentinel's own engine for comparison (see "
                          "stage-9-satellite-monitoring.md §9)",
                verbose_name='Computed by'),
        ),
    ]
