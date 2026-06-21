import os
import json
from rest_framework import status
from rest_framework.response import Response
from app.plugins.views import TaskView, GetTaskResult, TaskResultOutputError
from app.plugins.worker import run_function_async
from django.utils.translation import gettext_lazy as _

def compute_vegetation(orthophoto, index_name='ndvi', crop=None, progress_callback=None):
    import rasterio
    import numpy as np

    progress_callback("Loading orthophoto...", 10)

    with rasterio.open(orthophoto) as src:
        red = src.read(1).astype(float)
        green = src.read(2).astype(float)
        blue = src.read(3).astype(float)

        # Compute NDVI from RGB bands
        # normalized difference using green and red
        ndvi = (green - red) / (green + red + 1e-10)

        progress_callback("Computing index...", 50)

        # Sample on a grid
        height, width = ndvi.shape
        step = 50  # every 50 pixels
        features = []
        for y in range(0, height, step):
            for x in range(0, width, step):
                if ndvi[y, x] > -999:  # valid
                    lon, lat = src.xy(y, x)
                    features.append({
                        "type": "Feature",
                        "geometry": {
                            "type": "Point",
                            "coordinates": [lon, lat]
                        },
                        "properties": {
                            "index": ndvi[y, x]
                        }
                    })

        progress_callback("Creating GeoJSON...", 80)

        geojson = {
            "type": "FeatureCollection",
            "features": features
        }

        progress_callback("Done", 100)
        return {'output': json.dumps(geojson)}

class TaskVegetation(TaskView):
    def post(self, request, pk=None):
        task = self.get_and_check_task(request, pk)

        if task.orthophoto_extent is None:
            return Response({'error': _('No orthophoto is available.')})

        orthophoto = os.path.abspath(task.get_asset_download_path("orthophoto.tif"))
        index_name = request.data.get('index', 'ndvi')

        celery_task_id = run_function_async(compute_vegetation, orthophoto, index_name, task.crop.wkt if task.crop is not None else None, with_progress=True).task_id

        return Response({'celery_task_id': celery_task_id}, status=status.HTTP_200_OK)

class TaskVegetationDownload(GetTaskResult):
    def handle_output(self, output, result, **kwargs):
        try:
            return json.loads(output)
        except:
            raise TaskResultOutputError("Invalid GeoJSON")