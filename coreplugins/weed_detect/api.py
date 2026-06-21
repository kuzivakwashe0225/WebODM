import os
import json
from rest_framework import status
from rest_framework.response import Response
from app.plugins.views import TaskView, GetTaskResult, TaskResultOutputError
from app.plugins.worker import run_function_async
from django.utils.translation import gettext_lazy as _

def detect(orthophoto, model, classes=None, crop=None, progress_callback=None):
    import os
    import subprocess
    import shutil
    import tempfile
    from webodm import settings
    from django.contrib.gis.geos import GEOSGeometry

    try:
        from geodeep import detect as gdetect, models
        models.cache_dir = os.path.join(settings.MEDIA_CACHE, "detection_models")
    except ImportError:
        return {'error': "GeoDeep library is missing"}

    try:
        if crop is not None:
            # Make a VRT with the crop area

            gdalwarp_bin = shutil.which("gdalwarp")
            if gdalwarp_bin is None:
                return {'error': 'Cannot find gdalwarp'}
            
            tmpdir = os.path.join(settings.MEDIA_TMP, os.path.basename(tempfile.mkdtemp('_objdetect', dir=settings.MEDIA_TMP)))
    
            crop_geojson = os.path.join(tmpdir, "crop.geojson")
            ortho_vrt = os.path.join(tmpdir, "orthophoto.vrt")
            with open(crop_geojson, "w", encoding="utf-8") as f:
                f.write(GEOSGeometry(crop).geojson)
            p = subprocess.Popen([gdalwarp_bin, "-cutline", crop_geojson,
                    '--config', 'GDALWARP_DENSIFY_CUTLINE', 'NO', 
                    '-crop_to_cutline', '-of', 'VRT',
                    orthophoto, ortho_vrt], cwd=tmpdir, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            out, err = p.communicate()
            out = out.decode('utf-8').strip()
            err = err.decode('utf-8').strip()
            if p.returncode != 0:
                return {'error': f'Error calling gdalwarp: {str(err)}'}

            orthophoto = ortho_vrt

        if model == 'weeds':
            return weed_detect(orthophoto, progress_callback)

        return {'output': gdetect(orthophoto, model, output_type='geojson', classes=classes, max_threads=settings.WORKERS_MAX_THREADS, progress_callback=progress_callback)}
    except Exception as e:
        return {'error': str(e)}
     
class TaskWeedDetect(TaskView):
    def post(self, request, pk=None):
        task = self.get_and_check_task(request, pk)

        if task.orthophoto_extent is None:
            return Response({'error': _('No orthophoto is available.')})

        orthophoto = os.path.abspath(task.get_asset_download_path("orthophoto.tif"))

        celery_task_id = run_function_async(weed_detect, orthophoto, with_progress=True).task_id

        return Response({'celery_task_id': celery_task_id}, status=status.HTTP_200_OK)

class TaskWeedDownload(GetTaskResult):
    def handle_output(self, output, result, **kwargs):
        try:
            return json.loads(output)
        except:
            raise TaskResultOutputError("Invalid GeoJSON")

def weed_detect(orthophoto, progress_callback=None):
    import rasterio
    import numpy as np
    from scipy import ndimage

    progress_callback("Loading orthophoto...", 10)
    with rasterio.open(orthophoto) as src:
        red = src.read(1).astype(float)
        green = src.read(2).astype(float)
        blue = src.read(3).astype(float)

        # Compute NGRDI = (G - R) / (G + R)
        ngrdi = (green - red) / (green + red + 1e-10)

        progress_callback("Computing vegetation index...", 30)

        # Threshold for potential weeds (low vegetation)
        mask = (ngrdi > 0) & (ngrdi < 0.3)

        progress_callback("Segmenting plants...", 50)

        # Label connected components
        labeled, num_features = ndimage.label(mask)

        # Filter small objects (min area 10 pixels)
        if num_features > 0:
            sizes = ndimage.sum(mask, labeled, range(1, num_features + 1))
            min_size = 10
            filtered = sizes >= min_size
            filtered_labeled = np.where(filtered[labeled - 1], labeled, 0)

            # Get centroids
            centroids = ndimage.center_of_mass(mask, filtered_labeled, range(1, num_features + 1))
            valid_centroids = [c for i, c in enumerate(centroids) if filtered[i]]
        else:
            valid_centroids = []

        progress_callback("Converting to GeoJSON...", 80)

        # Convert to GeoJSON
        features = []
        for y, x in valid_centroids:
            lon, lat = src.xy(y, x)
            features.append({
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [lon, lat]
                },
                "properties": {
                    "class": "weed",
                    "score": 1.0
                }
            })

        geojson = {
            "type": "FeatureCollection",
            "features": features,
            "properties": {
                "count": len(features)
            }
        }

        progress_callback("Done", 100)
        return {'output': json.dumps(geojson)}
