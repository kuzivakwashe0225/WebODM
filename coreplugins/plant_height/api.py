import os
import json
from rest_framework import status
from rest_framework.response import Response
from app.plugins.views import TaskView, GetTaskResult, TaskResultOutputError
from app.plugins.worker import run_function_async
from django.utils.translation import gettext_lazy as _

def compute_height(dsm, progress_callback=None):
    import rasterio
    import numpy as np

    progress_callback("Loading DSM...", 10)

    with rasterio.open(dsm) as src:
        elevation = src.read(1).astype(float)

        # Compute relative height (elevation - min)
        min_elev = np.nanmin(elevation)
        height = elevation - min_elev

        progress_callback("Computing heights...", 50)

        # Sample on a grid
        h_arr, width = height.shape
        step = 50  # every 50 pixels
        features = []
        for y in range(0, h_arr, step):
            for x in range(0, width, step):
                h = height[y, x]
                if not np.isnan(h):
                    lon, lat = src.xy(y, x)
                    features.append({
                        "type": "Feature",
                        "geometry": {
                            "type": "Point",
                            "coordinates": [lon, lat]
                        },
                        "properties": {
                            "height": h
                        }
                    })

        progress_callback("Creating GeoJSON...", 80)

        geojson = {
            "type": "FeatureCollection",
            "features": features
        }

        progress_callback("Done", 100)
        return {'output': json.dumps(geojson)}

class TaskHeight(TaskView):
    def post(self, request, pk=None):
        task = self.get_and_check_task(request, pk)

        if task.dsm_extent is None:
            return Response({'error': _('No DSM is available. To estimate plant height you need a DSM.')})

        dsm = os.path.abspath(task.get_asset_download_path("dsm.tif"))

        celery_task_id = run_function_async(compute_height, dsm, with_progress=True).task_id

        return Response({'celery_task_id': celery_task_id}, status=status.HTTP_200_OK)

class TaskHeightDownload(GetTaskResult):
    def handle_output(self, output, result, **kwargs):
        try:
            return json.loads(output)
        except:
            raise TaskResultOutputError("Invalid GeoJSON")