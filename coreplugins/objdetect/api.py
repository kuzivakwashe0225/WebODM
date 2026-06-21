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

        if model == 'crops':
            import numpy as np
            from scipy import ndimage
            from rasterio.enums import ColorInterp

            def read_rgb(src):
                color_map = {}
                try:
                    for idx, interp in enumerate(src.colorinterp, start=1):
                        color_map[interp] = idx
                except Exception:
                    color_map = {}
                red_idx = color_map.get(ColorInterp.red, 1)
                green_idx = color_map.get(ColorInterp.green, 2)
                blue_idx = color_map.get(ColorInterp.blue, 3)
                if src.count < max(red_idx, green_idx, blue_idx):
                    raise ValueError("The orthophoto needs at least three RGB bands.")
                return (
                    src.read(red_idx, masked=True).astype(np.float32),
                    src.read(green_idx, masked=True).astype(np.float32),
                    src.read(blue_idx, masked=True).astype(np.float32),
                )

            def update_progress(message, value):
                if callable(progress_callback):
                    progress_callback(message, value)

            import rasterio
            import json as _json
            update_progress("Loading orthophoto...", 10)
            with rasterio.open(orthophoto) as src:
                red, green, blue = read_rgb(src)
                valid_mask = ~(red.mask | green.mask | blue.mask)
                red = np.asarray(red.filled(0), dtype=np.float32)
                green = np.asarray(green.filled(0), dtype=np.float32)
                blue = np.asarray(blue.filled(0), dtype=np.float32)
                update_progress("Computing vegetation index...", 30)
                ngrdi = (green - red) / (green + red + 1e-6)
                vari = (green - red) / (green + red - blue + 1e-6)
                exg = (2.0 * green) - red - blue
                vegetation_score = (0.45 * ngrdi) + (0.35 * vari) + (0.20 * (exg / 255.0))
                mask = valid_mask & (vegetation_score > 0.08) & (green > red * 0.9)
                mask = ndimage.binary_opening(mask, structure=np.ones((3, 3), dtype=bool))
                mask = ndimage.binary_closing(mask, structure=np.ones((5, 5), dtype=bool))
                mask = ndimage.binary_fill_holes(mask)
                update_progress("Segmenting plants...", 55)
                vegetation_pixels = int(mask.sum())
                if vegetation_pixels == 0:
                    geojson = {"type": "FeatureCollection", "features": [], "properties": {"count": 0}}
                    update_progress("Done", 100)
                    return {'output': _json.dumps(geojson)}
                min_area = max(9, int(vegetation_pixels * 0.00002))
                distance = ndimage.distance_transform_edt(mask)
                local_max = distance == ndimage.maximum_filter(distance, size=7)
                seed_mask = local_max & mask & (distance >= 2.0)
                seed_labels, seed_count = ndimage.label(seed_mask)
                valid_centroids = []
                if seed_count > 0:
                    for label_id, seed_slice in enumerate(ndimage.find_objects(seed_labels), start=1):
                        if seed_slice is None:
                            continue
                        component_mask = seed_labels[seed_slice] == label_id
                        center_y, center_x = ndimage.center_of_mass(component_mask)
                        y = seed_slice[0].start + center_y
                        x = seed_slice[1].start + center_x
                        y0 = max(0, int(y) - 10)
                        y1 = min(mask.shape[0], int(y) + 11)
                        x0 = max(0, int(x) - 10)
                        x1 = min(mask.shape[1], int(x) + 11)
                        local_area = int(mask[y0:y1, x0:x1].sum())
                        if local_area >= min_area:
                            valid_centroids.append((float(y), float(x)))
                if not valid_centroids:
                    labeled, num_features = ndimage.label(mask)
                    if num_features > 0:
                        sizes = ndimage.sum(mask, labeled, range(1, num_features + 1))
                        centroids = ndimage.center_of_mass(mask, labeled, range(1, num_features + 1))
                        valid_centroids = [
                            c for i, c in enumerate(centroids)
                            if sizes[i] >= min_area and not np.isnan(c[0]) and not np.isnan(c[1])
                        ]
                deduped_centroids = []
                for centroid in valid_centroids:
                    if all(np.hypot(centroid[0] - y, centroid[1] - x) >= 5.0 for y, x in deduped_centroids):
                        deduped_centroids.append(centroid)
                valid_centroids = deduped_centroids
                update_progress("Converting to GeoJSON...", 85)
                features = []
                for y, x in valid_centroids:
                    lon, lat = src.xy(y, x)
                    features.append({
                        "type": "Feature",
                        "geometry": {"type": "Point", "coordinates": [lon, lat]},
                        "properties": {"class": "plant", "score": 1.0}
                    })
                geojson = {
                    "type": "FeatureCollection",
                    "features": features,
                    "properties": {"count": len(features)}
                }
                update_progress("Done", 100)
                return {'output': _json.dumps(geojson)}

        try:
            from geodeep import detect as gdetect, models
            models.cache_dir = os.path.join(settings.MEDIA_CACHE, "detection_models")
        except ImportError:
            return {'error': "GeoDeep library is missing"}
        return {'output': gdetect(orthophoto, model, output_type='geojson', classes=classes, max_threads=settings.WORKERS_MAX_THREADS, progress_callback=progress_callback)}
    except Exception as e:
        return {'error': str(e)}
     
class TaskObjDetect(TaskView):
    def post(self, request, pk=None):
        task = self.get_and_check_task(request, pk)

        if task.orthophoto_extent is None:
            return Response({'error': _('No orthophoto is available.')})

        orthophoto = os.path.abspath(task.get_asset_download_path("orthophoto.tif"))
        model = request.data.get('model', 'cars')

        # model --> (modelID, classes)
        model_map = {
            'cars': ('cars', None),
            'trees': ('trees', None),
            'athletic': ('aerovision', ['tennis-court', 'track-field', 'soccer-field', 'baseball-field', 'swimming-pool', 'basketball-court']),
            'boats': ('aerovision', ['boat']),
            'planes': ('aerovision', ['plane']),
            'crops': ('crops', None),
        }

        if not model in model_map:
            return Response({'error': 'Invalid model'}, status=status.HTTP_200_OK)

        model_id, classes = model_map[model]
        celery_task_id = run_function_async(detect, orthophoto, model_id, classes, task.crop.wkt if task.crop is not None else None, with_progress=True).task_id

        return Response({'celery_task_id': celery_task_id}, status=status.HTTP_200_OK)

class TaskObjDownload(GetTaskResult):
    def handle_output(self, output, result, **kwargs):
        try:
            return json.loads(output)
        except:
            raise TaskResultOutputError("Invalid GeoJSON")
